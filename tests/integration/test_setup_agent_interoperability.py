"""Issue #335: Setup-to-first-task interoperability with measured Agent adapters."""

import asyncio
import copy
import json

import httpx
import pytest

from core.policy import ToolPolicy
from packages.contracts.models import SessionRequest, TaskRequest, TaskState
from packages.providers.lmstudio import LMStudioProvider
from packages.providers.text import CompatibleTextProvider, MetaResponsesProvider
from services import launcher
from services.agent import AgentRuntime
from services.agent.artifacts import ArtifactStore
from services.distribution import installation, wizard
from services.session.store import LocalJournal, Store


class _ControlledLMStudioWire:
    """Deterministic local auth-v1 socket fixture for setup-to-first-task verification."""

    def __init__(self, model_id: str, *, prompt_tokens: int = 42):
        self.model_id = model_id
        self.prompt_tokens = prompt_tokens
        self.connections: list[_ControlledLMStudioSocket] = []
        self.rpc_endpoints: list[str] = []
        self.predictions: list[dict] = []
        self.model_info = {
            "type": "llm",
            "identifier": model_id,
            "instanceReference": f"instance-{model_id}",
            "path": f"public/{model_id}.gguf",
            "modelKey": model_id,
            "indexedModelIdentifier": f"public/{model_id}.gguf",
            "format": "gguf",
            "deviceIdentifier": None,
        }
        self.load_config = {"fields": [{"key": "llm.load.contextLength", "value": 4096}]}
        self.base_config = {"fields": [{"key": "llm.prediction.systemPrompt", "value": ""}]}

    async def connect(self, endpoint: str):
        sock = _ControlledLMStudioSocket(self, endpoint)
        self.connections.append(sock)
        return sock


class _ControlledLMStudioSocket:
    def __init__(self, wire: _ControlledLMStudioWire, endpoint: str):
        self.wire = wire
        self.endpoint = endpoint
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.waited = False

    def _put(self, packet: dict):
        self.queue.put_nowait(json.dumps(packet))

    async def send(self, raw: str):
        packet = json.loads(raw)
        if "authVersion" in packet:
            self._put({"success": True})
            return
        if packet.get("type") == "rpcCall":
            ep = packet["endpoint"]
            self.wire.rpc_endpoints.append(ep)
            if ep == "listLoaded":
                result = [copy.deepcopy(self.wire.model_info)]
            elif ep == "getModelInfo":
                result = copy.deepcopy(self.wire.model_info)
            elif ep == "getLoadConfig":
                result = copy.deepcopy(self.wire.load_config)
            elif ep == "getBasePredictionConfig":
                result = copy.deepcopy(self.wire.base_config)
            elif ep == "applyPromptTemplate":
                result = {"formatted": "<|im_start|>user\nsynthetic<|im_end|>\n"}
            elif ep == "countTokens":
                result = {"tokenCount": self.wire.prompt_tokens}
            else:
                raise AssertionError(f"Unexpected RPC endpoint: {ep}")
            self._put({"type": "rpcResult", "callId": packet["callId"], "result": result})
            return
        if packet.get("type") == "channelCreate":
            assert packet["endpoint"] == "predict"
            creation = packet["creationParameter"]
            self.wire.predictions.append(copy.deepcopy(creation))
            fields = {}
            for layer in creation["predictionConfigStack"]["layers"]:
                fields.update({f["key"]: f["value"] for f in layer["config"]["fields"]})
            step_reply = (
                json.dumps(
                    {
                        "tool": "read_file",
                        "arguments": {"path": "readme.txt"},
                    }
                )
                if len(self.wire.predictions) == 1
                else json.dumps(
                    {
                        "tool": "finish",
                        "arguments": {
                            "summary": "Completed first setup task.",
                            "objective_revision": 1,
                        },
                    }
                )
            )
            self._put(
                {
                    "type": "channelSend",
                    "channelId": 1,
                    "message": {
                        "type": "fragment",
                        "fragment": {
                            "content": step_reply,
                            "tokensCount": 8,
                            "containsDrafted": False,
                            "reasoningType": "none",
                            "isStructural": False,
                        },
                    },
                }
            )
            self._put(
                {
                    "type": "channelSend",
                    "channelId": 1,
                    "message": {
                        "type": "success",
                        "stats": {
                            "stopReason": "eosFound",
                            "promptTokensCount": self.wire.prompt_tokens,
                            "predictedTokensCount": 8,
                            "totalTokensCount": self.wire.prompt_tokens + 8,
                        },
                        "modelInfo": copy.deepcopy(self.wire.model_info),
                        "loadModelConfig": copy.deepcopy(self.wire.load_config),
                        "predictionConfig": {
                            "fields": [{"key": k, "value": v} for k, v in fields.items()]
                        },
                    },
                }
            )
            self._put({"type": "channelClose", "channelId": 1})
            return
        raise AssertionError(f"Unexpected packet: {packet}")

    async def recv(self):
        return await self.queue.get()

    async def close(self):
        self.closed = True

    async def wait_closed(self):
        self.waited = True


async def test_setup_to_first_permitted_agent_task_with_local_lmstudio_adapter(
    tmp_path, monkeypatch
):
    layout = installation.Layout.for_home(tmp_path)
    result = installation.setup(
        layout,
        endpoint="http://127.0.0.1:1234/v1",
        model="qwen2.5-coder-7b",
        adapter="lmstudio",
    )
    assert result == {
        "ready": False,
        "provider_connection": "not_checked",
        "next": "kinaiya serve host",
    }
    config = json.loads(layout.config.read_text())
    assert config["providers"]["agent"]["kind"] == "lmstudio"
    assert config["providers"]["converse"]["kind"] == "compatible"
    assert config["providers"]["decisions"]["kind"] == "compatible"

    adapter = launcher.provider(config, "agent")
    assert isinstance(adapter, LMStudioProvider)
    assert callable(adapter.prepare_complete)

    wire = _ControlledLMStudioWire("qwen2.5-coder-7b")
    adapter._transport_factory = wire.connect
    (layout.workspace / "readme.txt").write_text("synthetic workspace fixture\n")

    store = Store(layout.data / "journal.sqlite")
    artifacts = ArtifactStore(layout.data / "artifacts.sqlite3")
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        adapter,
        layout.workspace,
        ToolPolicy(layout.workspace),
        artifact_store=artifacts,
    )
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        accepted = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="Verify setup-to-first-task interoperability",
                content_scope="public_synthetic",
                idempotency_key="setup-first-task-local",
            )
        )
        record = runtime._tasks[accepted.task_id]
        await asyncio.wait_for(record.done.wait(), timeout=5.0)
        assert record.task.state == TaskState.SUCCEEDED
        assert "countTokens" in wire.rpc_endpoints
        assert len(wire.predictions) == 2
    finally:
        await runtime.close()
        await adapter.close()
        store.close()


async def test_setup_to_first_permitted_agent_task_with_cloud_meta_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_META_KEY", "synthetic-meta-token")
    layout = installation.Layout.for_home(tmp_path)
    result = installation.setup(
        layout,
        endpoint="https://api.meta.ai/v1",
        model="muse-spark-1.3-contributor",
        adapter="meta",
        allow_cloud=True,
        credential_env="SYNTHETIC_META_KEY",
        context_tokens=32768,
    )
    assert result["ready"] is False
    config = json.loads(layout.config.read_text())
    assert config["providers"]["agent"]["kind"] == "meta"
    assert config["providers"]["agent"]["context_tokens"] == 32768
    assert "synthetic-meta-token" not in layout.config.read_text()

    counts = []
    generations = []

    def meta_transport(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/v1/responses/input_tokens":
            assert body["stream"] is False
            counts.append(body)
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 64})
        assert request.url.path == "/v1/responses"
        assert counts and body == counts[-1]
        generations.append(body)
        step_text = (
            json.dumps(
                {
                    "tool": "read_file",
                    "arguments": {"path": "readme.txt"},
                }
            )
            if len(generations) == 1
            else json.dumps(
                {
                    "tool": "finish",
                    "arguments": {
                        "summary": "Cloud setup task finished.",
                        "objective_revision": 1,
                    },
                }
            )
        )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": step_text}],
                    }
                ],
            },
        )

    adapter = launcher.provider(config, "agent")
    assert isinstance(adapter, MetaResponsesProvider)
    assert callable(adapter.prepare_complete)
    assert adapter.cloud_prompt_measurement is True
    await adapter.http.aclose()
    from packages.providers.complete_http import RequestBoundAsyncClient

    adapter.http = RequestBoundAsyncClient(
        base_url="https://api.meta.ai/v1/",
        timeout=120,
        headers={
            "Authorization": "Bearer synthetic-meta-token",
            "x-tbh-contributor-mode": "true",
        },
        transport=httpx.MockTransport(meta_transport),
        trust_env=False,
        follow_redirects=False,
    )

    (layout.workspace / "readme.txt").write_text("synthetic cloud workspace fixture\n")
    store = Store(layout.data / "journal.sqlite")
    artifacts = ArtifactStore(layout.data / "artifacts.sqlite3")
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        adapter,
        layout.workspace,
        ToolPolicy(layout.workspace),
        artifact_store=artifacts,
    )
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        accepted = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="Verify cloud setup-to-first-task interoperability",
                content_scope="public_synthetic",
                idempotency_key="setup-first-task-cloud",
            )
        )
        record = runtime._tasks[accepted.task_id]
        await asyncio.wait_for(record.done.wait(), timeout=5.0)
        assert record.task.state == TaskState.SUCCEEDED
        assert len(counts) == 2
        assert len(generations) == 2
    finally:
        await runtime.close()
        await adapter.close()
        store.close()


async def test_healthy_synthetic_endpoint_with_unsupported_adapter_interface_is_refused(
    tmp_path,
):
    """A healthy OpenAI-compatible endpoint lacking prepare_complete is refused with guidance."""
    layout = installation.Layout.for_home(tmp_path)

    def healthy_openai_endpoint(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "healthy-local-model"}]})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    # Even though the synthetic endpoint is healthy, selecting 'compatible' for Agent
    # in setup or wizard is refused with actionable remediation before writing files.
    with pytest.raises(
        installation.InstallError,
        match=r"prepare_complete.*select 'lmstudio'.*or 'meta'",
    ):
        installation.setup(
            layout,
            endpoint="http://127.0.0.1:8080/v1",
            model="healthy-local-model",
            adapter="compatible",
        )
    assert not layout.config.exists() and not layout.data.exists()

    # Interactive wizard also refuses 'compatible=...' with the same actionable guidance.
    class _TTY:
        def __init__(self, answers):
            self.answers = iter(answers)
            self.output = ""

        def write(self, text):
            self.output += text

        def flush(self):
            pass

        def readline(self):
            return next(self.answers) + "\n"

    tty = _TTY(
        [
            "",
            "",
            "compatible=http://127.0.0.1:8080/v1",
            "healthy-local-model",
            "no",
            "",
            "no",
            "yes",
        ]
    )
    with pytest.raises(
        installation.InstallError,
        match=r"prepare_complete.*select 'lmstudio'.*or 'meta'",
    ):
        wizard.run_wizard(layout, tty=tty)
    assert not layout.config.exists()

    # And an unmeasured CompatibleTextProvider bound to the healthy transport is refused
    # by Agent complete-request admission without calling the endpoint.
    unmeasured = CompatibleTextProvider(
        "http://127.0.0.1:8080/v1",
        "healthy-local-model",
        transport=httpx.MockTransport(healthy_openai_endpoint),
    )
    assert not hasattr(unmeasured, "prepare_complete")
    store = Store(tmp_path / "journal.sqlite")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        unmeasured,
        workspace,
        ToolPolicy(workspace),
        artifact_store=ArtifactStore(tmp_path / "artifacts.sqlite3"),
    )
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        accepted = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="Attempt task on unmeasured compatible adapter",
                content_scope="public_synthetic",
                idempotency_key="unmeasured-refusal",
            )
        )
        record = runtime._tasks[accepted.task_id]
        await asyncio.wait_for(record.done.wait(), timeout=5.0)
        assert record.task.state == TaskState.FAILED
        assert "Agent provider cannot measure complete requests" in (record.task.error or "")
    finally:
        await runtime.close()
        await unmeasured.close()
        store.close()


def test_interactive_and_noninteractive_setup_preserve_independent_roles_and_existing_data(
    tmp_path,
):
    # 1. Noninteractive setup with independent role configurations.
    layout_non_tty = installation.Layout.for_home(tmp_path / "non-tty")
    layout_non_tty.data.mkdir(parents=True)
    (layout_non_tty.data / "owner-note.txt").write_text("preserve-me")
    installation.setup(
        layout_non_tty,
        roles={
            "agent": {
                "kind": "lmstudio",
                "endpoint": "http://127.0.0.1:1234",
                "model": "agent-coder-model",
            },
            "converse": {
                "kind": "compatible",
                "endpoint": "http://127.0.0.1:8081/v1",
                "model": "converse-dialogue-model",
            },
            "decisions": {
                "kind": "native_d1",
                "endpoint": "http://127.0.0.1:8082",
                "model": "lfm3-d1",
            },
        },
        converse=True,
    )
    cfg = json.loads(layout_non_tty.config.read_text())
    assert cfg["providers"]["agent"]["kind"] == "lmstudio"
    assert cfg["providers"]["agent"]["model"] == "agent-coder-model"
    assert cfg["providers"]["converse"]["kind"] == "compatible"
    assert cfg["providers"]["converse"]["model"] == "converse-dialogue-model"
    assert cfg["providers"]["decisions"]["kind"] == "native_d1"
    assert cfg["providers"]["decisions"]["model"] == "lfm3-d1"
    assert (layout_non_tty.data / "owner-note.txt").read_text() == "preserve-me"

    # Re-running setup refuses without altering existing config or data.
    saved_bytes = layout_non_tty.config.read_bytes()
    with pytest.raises(installation.InstallError, match="Preserving existing configuration"):
        installation.setup(
            layout_non_tty,
            endpoint="http://127.0.0.1:1234",
            model="different-model",
        )
    assert layout_non_tty.config.read_bytes() == saved_bytes
    assert (layout_non_tty.data / "owner-note.txt").read_text() == "preserve-me"

    # 2. Interactive wizard setup with independent Converse and Decisions role choices.
    layout_tty = installation.Layout.for_home(tmp_path / "tty")

    class _TTY:
        def __init__(self, answers):
            self.answers = iter(answers)
            self.output = ""

        def write(self, text):
            self.output += text

        def flush(self):
            pass

        def readline(self):
            return next(self.answers) + "\n"

    tty = _TTY(
        [
            "",
            "",
            "lmstudio=http://127.0.0.1:1234",
            "agent-local-model",
            "no",
            "",
            "yes",
            "compatible",
            "http://127.0.0.1:8081/v1",
            "converse-local-model",
            "native_d1",
            "http://127.0.0.1:8082",
            "decisions-d1-model",
            "yes",
        ]
    )
    wizard.run_wizard(layout_tty, tty=tty)
    tty_cfg = json.loads(layout_tty.config.read_text())
    assert tty_cfg["providers"]["agent"]["kind"] == "lmstudio"
    assert tty_cfg["providers"]["agent"]["model"] == "agent-local-model"
    assert tty_cfg["providers"]["converse"]["kind"] == "compatible"
    assert tty_cfg["providers"]["converse"]["model"] == "converse-local-model"
    assert tty_cfg["providers"]["decisions"]["kind"] == "native_d1"
    assert tty_cfg["providers"]["decisions"]["model"] == "decisions-d1-model"
    assert tty_cfg["distribution"]["converse"] is True


@pytest.mark.parametrize(
    "model_name",
    [
        "muse-spark-1.3-contributor",
        "gpt-4o-mini",
        "lfm3-d1",
        "qwen2.5-coder-7b",
    ],
)
def test_setup_never_infers_provider_kind_from_model_name(tmp_path, model_name):
    layout = installation.Layout.for_home(tmp_path / model_name)
    installation.setup(
        layout,
        endpoint="http://127.0.0.1:1234",
        model=model_name,
        adapter="lmstudio",
    )
    cfg = json.loads(layout.config.read_text())
    assert cfg["providers"]["agent"]["kind"] == "lmstudio"
    assert cfg["providers"]["agent"]["model"] == model_name
