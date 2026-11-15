"""Owner preferences reach real classifier requests without changing native actions."""

import hashlib
import json
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.decisions_prompts import DecisionsPromptConfig
from packages.contracts.models import AppendRequest, SessionRequest, SpeechWindow, Task
from packages.providers.text import CompatibleTextProvider, ProviderRefusal
from services import launcher
from services.decisions.prompts import DEFAULT_PROMPT, configured_prompt, instructions, prompt_stamp
from services.decisions.runtime import DecisionsWorker, ModelClassifier
from services.decisions.window_routing import WindowClassifier
from services.decisions.window_worker import WindowDecisionsWorker
from services.session.store import LocalJournal, Store


def custom(text="Public custom preference Ω", scope="public_synthetic"):
    return DecisionsPromptConfig(finalized=text, window=text, content_scope=scope)


def test_defaults_and_blank_override_are_distinct_and_frozen():
    assert configured_prompt({}) is configured_prompt({"decisions_prompt": None}) is DEFAULT_PROMPT
    assert "genuine user questions and commands" in DEFAULT_PROMPT.finalized
    assert "High priority need not mean immediate interruption" in DEFAULT_PROMPT.window
    assert "status question alone does not request restarting" in DEFAULT_PROMPT.window
    blank = configured_prompt({"decisions_prompt": {"finalized": "", "window": ""}})
    assert blank.content_scope == "local_only"
    for mode in ("finalized", "window"):
        selected = instructions(blank, mode)
        assert selected.endswith("Owner interpretation preferences:\n")
        assert "genuine user questions" not in selected
        assert "Return ONLY JSON" in selected
    with pytest.raises(ValueError):
        blank.window = "changed"


@pytest.mark.parametrize(
    "update",
    [
        {"contract_version": True},
        {"contract_version": 2},
        {"finalized": None},
        {"window": 3},
        {"window": "é" * 4097},
        {"finalized": "\ud800"},
        {"window": "\x00PRIVATE_PROMPT_FIXTURE"},
        {"content_scope": "public"},
        {"unknown": "PRIVATE_PROMPT_FIXTURE"},
    ],
)
def test_malformed_prompt_is_redacted(update):
    value = {"finalized": "PRIVATE_PROMPT_FIXTURE", "window": ""} | update
    with pytest.raises(ValueError) as caught:
        configured_prompt({"decisions_prompt": value})
    assert str(caught.value) == "Invalid owner-configured Decisions prompt"


def test_utf8_boundary_and_independent_schema_export():
    assert custom("é" * 4096).finalized == "é" * 4096
    path = (
        Path(__file__).resolve().parents[2]
        / "packages/contracts/schemas/DecisionsPromptConfig.json"
    )
    raw = path.read_bytes()
    assert json.loads(raw) == DecisionsPromptConfig.model_json_schema()
    assert (
        hashlib.sha256(raw).hexdigest()
        == "e1b42ec990beb4834146889d866c3121ec23f7632f46fb1e3b4fe7d02562f0bc"
    )


def source(store, session, mode, text):
    payload = {"text": text, "content_scope": "public_synthetic", "final": True}
    kind = "conversation.user"
    if mode == "window":
        kind = "conversation.window"
        payload = SpeechWindow(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            utterance_id="public-utterance",
            window_index=0,
            text=text,
            start_ms=0,
            end_ms=1000,
            user_speaking=False,
            capture_ended=True,
            content_scope="public_synthetic",
        ).model_dump(mode="json")
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind=kind,
            payload=payload,
            idempotency_key="public-prompt-fixture",
        ),
        "owner",
    )


def response(mode):
    labels = (
        {"route": "ignore", "reason_code": "illustrative_fixture"}
        if mode == "finalized"
        else {
            "conversation": "RESPOND_WITHOUT_INTERRUPTING_USER",
            "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
            "target_task_id": None,
            "expected_objective_revision": None,
            "reason_code": "illustrative_fixture",
        }
    )
    return httpx.Response(
        200,
        stream=httpx.ByteStream(
            json.dumps(
                {"choices": [{"message": {"content": json.dumps(labels)}, "finish_reason": "stop"}]}
            ).encode()
        ),
    )


CASES = (
    "Prepare a public museum opening checklist.",
    "Change the opening time to ten.",
    "What is the status of that checklist?",
    "I like that painting.",
    "Who doesn't enjoy a sunny day?",
)


@pytest.mark.parametrize("mode", ["finalized", "window"])
@pytest.mark.parametrize("text", CASES)
async def test_illustrative_matrix_transmits_policy_and_source_without_keyword_override(
    tmp_path, mode, text
):
    seen = []

    def wire(request):
        seen.append(json.loads(request.content))
        return response(mode)

    provider = CompatibleTextProvider(
        "http://localhost/v1", "fixture", transport=httpx.MockTransport(wire)
    )
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        entry = source(store, session, mode, text)
        task = Task(
            task_id="public-task",
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            objective="Public checklist",
            state="running",
            content_scope="public_synthetic",
            objective_revision=2,
            idempotency_key="public-task-request",
        )
        classifier = (
            ModelClassifier(provider) if mode == "finalized" else WindowClassifier(provider)
        )
        result = await classifier.classify(entry, [], [task])
        assert result.get("route", result.get("agent_route")) in {
            "ignore",
            "IGNORE_FOR_IMMEDIATE_ROUTING",
        }
        assert len(seen) == 1
        messages = seen[0]["messages"]
        assert messages[0]["content"] == instructions(DEFAULT_PROMPT, mode)
        assert json.loads(messages[1]["content"])["latest"]["text"] == text
        assert store.resolve(entry.event_id).payload["text"] == text
    finally:
        await provider.close()
        store.close()


@pytest.mark.parametrize("mode", ["finalized", "window"])
async def test_selected_model_lease_preserves_exact_override(tmp_path, mode):
    seen = []

    def wire(request):
        seen.append(json.loads(request.content))
        return response(mode)

    provider = CompatibleTextProvider(
        "http://localhost/v1", "fixture", transport=httpx.MockTransport(wire)
    )
    store = Store(tmp_path / "journal.sqlite")
    prompt = custom()

    class Models:
        def preparing(self, _):
            return nullcontext()

        @asynccontextmanager
        async def operation(self):
            yield SimpleNamespace(provider=provider, pin=None, current=lambda: True)

        def cache_stamp(self):
            return "public-model-stamp"

        def _state(self):
            return (None, None, None)

        def _selection_info(self, _):
            return provider.info, None

    class Agent:
        async def status(self):
            return []

    try:
        session = store.create_session(SessionRequest(channel="converse"))
        entry = source(store, session, mode, CASES[0])
        journal = LocalJournal(store, "decisions")
        if mode == "finalized":
            worker = DecisionsWorker(
                journal, Agent(), ModelClassifier(provider, prompt=prompt), model_settings=Models()
            )
            await worker._classify(entry, [], [])
        else:
            worker = WindowDecisionsWorker(
                journal, Agent(), WindowClassifier(provider, prompt=prompt), model_settings=Models()
            )
            await worker.classify(entry)
        assert len(seen) == 1
        assert seen[0]["messages"][0]["content"] == instructions(prompt, mode)
        assert "genuine user questions" not in seen[0]["messages"][0]["content"]
    finally:
        await provider.close()
        store.close()


def host(monkeypatch, tmp_path, prompt):
    config = {
        "session_url": "http://127.0.0.1:8760",
        "agent_url": "http://127.0.0.1:8761",
        "decisions_prompt": prompt,
    }
    monkeypatch.setattr(launcher, "load_config", lambda _: config)
    monkeypatch.setattr(
        launcher, "load_or_create_tokens", lambda _: {r: r[0] * 32 for r in ("owner", "decisions")}
    )
    return SimpleNamespace(
        config=tmp_path / "config.json",
        data_dir=tmp_path / "data",
        workspace=tmp_path / "workspace",
        service="decisions",
        port=None,
    )


def test_invalid_launcher_prompt_stops_before_provider_or_catalog(monkeypatch, tmp_path):
    from services.settings.provider_catalog import ProviderCatalog

    args = host(
        monkeypatch, tmp_path, {"finalized": "Private fixture", "window": "", "contract_version": 2}
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid prompt must fail before constructing provider/catalog")

    monkeypatch.setattr(launcher, "provider", forbidden)
    monkeypatch.setattr(ProviderCatalog, "__init__", forbidden)
    with pytest.raises(ValueError, match="Invalid owner-configured Decisions prompt"):
        launcher.serve(args)


@pytest.mark.parametrize("text", ["", "Public custom host policy."])
def test_launcher_passes_exact_prompt_to_both_classifiers(monkeypatch, tmp_path, text):
    import services.decisions.model_settings
    import services.decisions.window_worker
    import services.settings.provider_catalog

    prompt = custom(text)
    args = host(monkeypatch, tmp_path, prompt.model_dump())
    captured = {}
    monkeypatch.setattr(launcher, "provider", lambda *a, **kw: object())
    monkeypatch.setattr(
        services.settings.provider_catalog, "ProviderCatalog", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        services.decisions.model_settings, "DecisionsModelSettings", lambda *a, **kw: object()
    )

    class StopConstruction(Exception):
        pass

    def worker(journal, agent, classifier, **kwargs):
        captured["finalized"] = classifier.prompt
        return SimpleNamespace(journal=journal, agent=agent, classifier=classifier, enabled=True)

    def windows(journal, agent, classifier, **kwargs):
        captured["window"] = classifier.prompt
        raise StopConstruction

    monkeypatch.setattr("services.decisions.runtime.DecisionsWorker", worker)
    monkeypatch.setattr(services.decisions.window_worker, "WindowDecisionsWorker", windows)
    with pytest.raises(StopConstruction):
        launcher.serve(args)
    assert captured == {"finalized": prompt, "window": prompt}


def test_private_prompt_stamp_refuses_cloud_before_hashing():
    with pytest.raises(ProviderRefusal, match="instructions do not permit"):
        prompt_stamp(
            custom(scope="local_only"),
            "window",
            SimpleNamespace(local=False, provider="fixture", model="fixture"),
        )


@pytest.mark.parametrize("mode", ["finalized", "window"])
@pytest.mark.parametrize("scope", ["local_only", "public_synthetic", "private_cloud_authorized"])
async def test_actual_cloud_request_requires_prompt_disclosure(tmp_path, mode, scope):
    seen = []

    def wire(request):
        seen.append(json.loads(request.content))
        return response(mode)

    provider = CompatibleTextProvider(
        "https://cloud.example.test/v1",
        "fixture",
        allow_cloud=True,
        transport=httpx.MockTransport(wire),
    )
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        entry = source(store, session, mode, CASES[0])
        prompt = custom(scope=scope)
        cls = ModelClassifier if mode == "finalized" else WindowClassifier
        classifier = cls(provider, prompt=prompt)
        if scope == "local_only":
            with pytest.raises(ProviderRefusal, match="instructions do not permit"):
                await classifier.classify(entry, [], [])
            assert seen == []
        else:
            await classifier.classify(entry, [], [])
            assert len(seen) == 1
            assert seen[0]["messages"][0]["content"] == instructions(prompt, mode)
    finally:
        await provider.close()
        store.close()


@pytest.mark.parametrize("mode", ["finalized", "window"])
@pytest.mark.parametrize("phase", ["audit_start", "last_session_read"])
async def test_prompt_scope_is_rechecked_after_native_capture_awaits(tmp_path, mode, phase):
    from packages.providers.decision_capture import decision_attempt

    seen = []
    provider = CompatibleTextProvider(
        "http://localhost/v1",
        "fixture",
        transport=httpx.MockTransport(lambda request: seen.append(request) or response(mode)),
    )
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        entry = source(store, session, mode, CASES[0])
        journal = LocalJournal(store, "decisions")
        method = "decisions_call_start" if phase == "audit_start" else "session"
        original = getattr(journal, method)

        async def changed(*args, **kwargs):
            value = await original(*args, **kwargs)
            provider.info.local = False
            return value

        setattr(journal, method, changed)
        cls = ModelClassifier if mode == "finalized" else WindowClassifier
        classifier = cls(provider, prompt=custom(scope="local_only"))
        with pytest.raises(ProviderRefusal, match="instructions do not permit"):
            async with decision_attempt(journal, entry, provider):
                await classifier.classify(entry, [], [])
        assert seen == []
    finally:
        await provider.close()
        store.close()
