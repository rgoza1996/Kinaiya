"""Issue #398: Clean Linux install-to-first-session onboarding qualification and regression suite."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import os
import stat
import tarfile
from contextlib import asynccontextmanager, contextmanager, redirect_stdout
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from core.policy import ToolPolicy
from packages.contracts.client import JournalClient
from packages.contracts.models import TaskState
from packages.providers.lmstudio import LMStudioProvider
from packages.providers.text import MetaResponsesProvider, ProviderError
from services import launcher
from services.agent import AgentRuntime
from services.agent.app import create_app as create_agent_app
from services.agent.artifacts import ArtifactStore
from services.common.auth import Auth, load_or_create_tokens
from services.distribution import cli as dist_cli
from services.distribution import installation, web, wizard
from services.session.api import create_app as create_session_app
from services.session.store import Store


def _load_bootstrap_module() -> ModuleType:
    script = Path(__file__).resolve().parents[2] / "install.sh"
    text = script.read_text(encoding="utf-8")
    source = text.split("# BEGIN PYTHON BOOTSTRAP\n", 1)[1].split("\n# END PYTHON BOOTSTRAP", 1)[0]
    module = ModuleType("kinaiya_bootstrap_qualification")
    exec(compile(source, str(script), "exec"), module.__dict__)
    return module


def _build_release_archive(
    tmp_path: Path,
    *,
    build: int = 1,
    channel: str = "nightly",
    include_web_assets: bool = True,
    flat: bool = False,
) -> tuple[Path, dict]:
    archive = tmp_path / f"kinaiya-{channel}-{build}-source.tar.gz"
    prefix = "" if flat else "kinaiya/"
    source_sha = f"{build:040x}"
    base_version = f"0.3.{build}"
    version = (
        base_version if channel == "stable" else f"{base_version}-nightly.{build}+{source_sha[:12]}"
    )
    identity = {
        "schema_version": 1,
        "channel": channel,
        "version": version,
        "build_number": build,
        "source_sha": source_sha,
        "protocol_min": 1,
        "protocol_max": 1,
        "state_schema": 1,
        "min_updater": 1,
    }
    members: dict[str, bytes] = {
        f"{prefix}pyproject.toml": b'[project]\nname = "kinaiya"\nversion = "0.3.1"\n',
        f"{prefix}requirements-runtime.lock": b"# hash-locked runtime wheels\n",
        f"{prefix}release-identity.json": json.dumps(identity, indent=2).encode("utf-8"),
        f"{prefix}services/launcher.py": b"# managed launcher entrypoint\n",
    }
    if include_web_assets:
        members[f"{prefix}apps/kinaiya_flutter/build/web/index.html"] = (
            b"<!doctype html><html><body>Kinaiya Owner UI</body></html>\n"
        )
        members[f"{prefix}apps/kinaiya_flutter/build/web/main.dart.js"] = (
            b"// compiled flutter web entrypoint\n"
        )
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    artifact_name = f"kinaiya-{version}-source.tar.gz"
    tag = f"v{version}" if channel == "stable" else f"nightly-{build}-{source_sha[:12]}"
    manifest = {
        **identity,
        "artifacts": [
            {
                "name": artifact_name,
                "platform": "linux",
                "architecture": "any",
                "url": f"https://github.com/rgoza1996/Kinaiya/releases/download/{tag}/{artifact_name}",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "size": archive.stat().st_size,
            }
        ],
    }
    return archive, manifest


class _ControlledTTY:
    """Deterministic /dev/tty double for interactive wizard qualification."""

    def __init__(self, answers: list[str]):
        self._answers = iter(answers)
        self.output = ""

    def write(self, text: str) -> int:
        self.output += text
        return len(text)

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        try:
            return next(self._answers) + "\n"
        except StopIteration:
            return ""


class _ControlledLMStudioWire:
    """Deterministic local auth-v1 socket fixture supporting prepare_complete."""

    def __init__(self, model_id: str, *, prompt_tokens: int = 48):
        self.model_id = model_id
        self.prompt_tokens = prompt_tokens
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
        return _ControlledLMStudioSocket(self, endpoint)


class _ControlledLMStudioSocket:
    def __init__(self, wire: _ControlledLMStudioWire, endpoint: str):
        self.wire = wire
        self.endpoint = endpoint
        self.queue: asyncio.Queue[str] = asyncio.Queue()

    def _put(self, packet: dict) -> None:
        self.queue.put_nowait(json.dumps(packet))

    async def send(self, raw: str) -> None:
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
                result = {"formatted": "<|im_start|>user\nonboarding<|im_end|>\n"}
            elif ep == "countTokens":
                result = {"tokenCount": self.wire.prompt_tokens}
            else:
                raise AssertionError(f"Unexpected RPC endpoint: {ep}")
            self._put({"type": "rpcResult", "callId": packet["callId"], "result": result})
            return
        if packet.get("type") == "channelCreate":
            creation = packet["creationParameter"]
            self.wire.predictions.append(copy.deepcopy(creation))
            fields = {}
            for layer in creation["predictionConfigStack"]["layers"]:
                fields.update({f["key"]: f["value"] for f in layer["config"]["fields"]})
            step_reply = (
                json.dumps({"tool": "read_file", "arguments": {"path": "onboarding.txt"}})
                if len(self.wire.predictions) == 1
                else json.dumps(
                    {
                        "tool": "finish",
                        "arguments": {
                            "summary": "Completed first onboarding session task.",
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

    async def recv(self) -> str:
        return await self.queue.get()

    async def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


@asynccontextmanager
async def _running_onboarding_stack(layout: installation.Layout, adapter):
    """Start real Session and Agent ASGI services backed by layout's SQLite stores and tokens."""
    config = json.loads(layout.config.read_text(encoding="utf-8"))
    tokens = load_or_create_tokens(layout.data)
    auth = Auth(tokens, tuple(config.get("origins", ())))
    store = Store(layout.data / "journal.sqlite")
    artifacts = ArtifactStore(layout.data / "artifacts.sqlite3")
    session_app = create_session_app(store, auth, agent_enabled=config.get("agent_enabled", True))
    session_transport = httpx.ASGITransport(app=session_app)
    journal = JournalClient(
        config["session_url"],
        tokens["agent"],
        transport=session_transport,
    )
    policy = ToolPolicy(
        layout.workspace,
        **{k: v for k, v in config.get("policy", {}).items() if k != "readonly_roots"},
    )
    runtime = AgentRuntime(
        journal,
        adapter,
        layout.workspace,
        policy,
        artifact_store=artifacts,
    )
    agent_app = create_agent_app(runtime, auth)
    agent_transport = httpx.ASGITransport(app=agent_app)
    try:
        async with (
            httpx.AsyncClient(
                transport=session_transport, base_url=config["session_url"]
            ) as session_http,
            httpx.AsyncClient(
                transport=agent_transport, base_url=config["agent_url"]
            ) as agent_http,
        ):
            yield SimpleNamespace(
                session_http=session_http,
                agent_http=agent_http,
                runtime=runtime,
                store=store,
                tokens=tokens,
                config=config,
            )
    finally:
        await asyncio.wait_for(runtime.close(), timeout=5.0)
        await asyncio.wait_for(journal.close(), timeout=5.0)
        await adapter.close()
        store.close()


async def test_clean_linux_install_to_first_session_end_to_end_journey(tmp_path, monkeypatch):
    """End-to-end journey: clean layout -> install -> wizard setup -> owner-login -> Session+Agent -> first task."""
    home = tmp_path / "clean-home"
    layout = installation.Layout.for_home(home)
    archive, release = _build_release_archive(tmp_path, build=1, channel="nightly")

    # 1. Stage verified release into clean layout using controlled runtime preparation.
    staged_generations: list[Path] = []
    install_result = installation.install_release(
        layout,
        release,
        archive,
        prepare=lambda gen, src: staged_generations.append(gen),
        busy=lambda: False,
    )
    assert install_result["source_sha"] == release["source_sha"]
    assert len(staged_generations) == 1
    assert (layout.install / "current").resolve() == staged_generations[0]
    assert (layout.bin / "kinaiya").is_file()

    # 2. Interactive wizard setup choosing local measured LM Studio adapter and skipping Converse.
    tty = _ControlledTTY(
        [
            "",  # default workspace
            "",  # default data dir
            "lmstudio=http://127.0.0.1:1234",
            "qwen2.5-coder-7b",
            "no",  # allow_cloud
            "",  # credential_env
            "no",  # converse
            "yes",  # confirm write
        ]
    )
    setup_result = wizard.run_wizard(layout, tty=tty)
    assert setup_result["ready"] is False
    assert setup_result["provider_connection"] == "not_checked"
    assert setup_result["next"] == "kinaiya serve host"
    assert "Provider connection is not checked" in tty.output

    config = json.loads(layout.config.read_text(encoding="utf-8"))
    assert config["distribution"] == {"converse": False}
    assert launcher.host_service_names(config) == ["session", "agent"]
    assert "voice" not in config

    # 3. Export owner login credential via CLI helper without manually editing service-tokens.json.
    owner_login_path = layout.data / "owner-login.txt"
    stdout_buf = io.StringIO()
    with redirect_stdout(stdout_buf):
        launcher.owner_login(SimpleNamespace(data_dir=layout.data, output=owner_login_path))
    owner_login_meta = json.loads(stdout_buf.getvalue())
    assert Path(owner_login_meta["path"]) == owner_login_path.resolve()
    assert owner_login_meta["mode"] == "0600"
    owner_token = owner_login_path.read_text(encoding="utf-8")
    assert len(owner_token) >= 24
    # Owner token value is never printed to stdout by owner-login.
    assert owner_token not in stdout_buf.getvalue()

    # 4. Wire controlled measured adapter and run first session + direct Agent task over ASGI stack.
    (layout.workspace / "onboarding.txt").write_text(
        "clean linux onboarding fixture\n", encoding="utf-8"
    )
    adapter = launcher.provider(config, "agent")
    assert isinstance(adapter, LMStudioProvider)
    wire = _ControlledLMStudioWire("qwen2.5-coder-7b")
    adapter._transport_factory = wire.connect

    async with _running_onboarding_stack(layout, adapter) as stack:
        owner_headers = {"Authorization": f"Bearer {owner_token}"}
        sess_resp = await stack.session_http.post(
            "/v1/sessions",
            json={"channel": "agent"},
            headers=owner_headers,
        )
        assert sess_resp.status_code == 200, sess_resp.text
        session_data = sess_resp.json()

        task_resp = await stack.agent_http.post(
            "/v1/agent/tasks",
            json={
                "session_id": session_data["session_id"],
                "thread_id": session_data["thread_id"],
                "epoch": session_data["epoch"],
                "objective": "Read onboarding.txt and complete first session qualification.",
                "content_scope": "local_only",
                "idempotency_key": "onboarding-first-task-1",
            },
            headers=owner_headers,
        )
        assert task_resp.status_code == 201, task_resp.text
        task_id = task_resp.json()["task_id"]

        final = await asyncio.wait_for(stack.runtime.wait(task_id, timeout=5.0), timeout=6.0)
        assert final.state == TaskState.SUCCEEDED, final.error
        assert "countTokens" in wire.rpc_endpoints
        assert len(wire.predictions) == 2

        status_resp = await stack.agent_http.get(
            f"/v1/agent/tasks/{task_id}",
            headers=owner_headers,
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["state"] == "succeeded"

        outcomes = stack.store.read(kinds=["agent.outcome"], task_id=task_id)
        assert len(outcomes) == 1
        assert outcomes[0].payload["task"]["state"] == "succeeded"


async def test_layout_and_configuration_matrix_spaces_idempotency_and_bootstrap_plan(
    tmp_path, monkeypatch
):
    """Matrix: default XDG vs custom paths with spaces, unchanged-layout rerun idempotency, and bootstrap dry plan."""
    # 1. Custom install roots with spaces in every directory name.
    custom_base = tmp_path / "My Kinaiya Install"
    spaced_layout = installation.Layout(
        install=custom_base / "Managed Install",
        data=custom_base / "Private Data",
        config=custom_base / "Config Dir" / "config.json",
        workspace=custom_base / "Owner Workspace",
        cache=custom_base / "Release Cache",
        bin=custom_base / "Local Bin",
    )
    spaced_layout.validate()

    archive, release = _build_release_archive(tmp_path, build=2, channel="nightly")
    prepare_calls: list[Path] = []
    first_install = installation.install_release(
        spaced_layout,
        release,
        archive,
        prepare=lambda gen, src: prepare_calls.append(gen),
        busy=lambda: False,
    )
    assert first_install["source_sha"] == release["source_sha"]
    assert len(prepare_calls) == 1

    # Verify launcher wrapper and generation launch script quote paths with spaces safely.
    wrapper_text = (spaced_layout.bin / "kinaiya").read_text(encoding="utf-8")
    launch_text = (spaced_layout.install / "current" / "launch").read_text(encoding="utf-8")
    assert "'/" in wrapper_text or "My Kinaiya Install" in wrapper_text
    assert "KINAIYA_INSTALL_ROOT=" in launch_text

    # Verify KINAIYA_INSTALL_ROOT resolves the spaced layout accurately via chosen_layout().
    monkeypatch.setenv("KINAIYA_INSTALL_ROOT", str(spaced_layout.install))
    assert dist_cli.chosen_layout() == spaced_layout
    assert set(launcher.managed_guard_roots()) >= {
        spaced_layout.install,
        spaced_layout.data,
        spaced_layout.config.parent,
        spaced_layout.cache,
        spaced_layout.bin,
    }

    # 2. Setup on spaced layout with skipped Converse.
    setup_out = installation.setup(
        spaced_layout,
        endpoint="http://127.0.0.1:1234",
        model="local-spaced-model",
        adapter="lmstudio",
        converse=False,
    )
    assert setup_out["ready"] is False
    saved_config_bytes = spaced_layout.config.read_bytes()
    (spaced_layout.workspace / "keep.txt").write_text("workspace data intact", encoding="utf-8")

    # 3. Unchanged-layout rerun idempotency: reinstalling the identical release is a no-op ("current").
    second_install = installation.install_release(
        spaced_layout,
        release,
        archive,
        prepare=lambda *a: pytest.fail("unchanged release must not re-stage"),
        busy=lambda: False,
    )
    assert second_install == release
    assert len(list((spaced_layout.install / "generations").iterdir())) == 1
    assert spaced_layout.config.read_bytes() == saved_config_bytes
    assert (spaced_layout.workspace / "keep.txt").read_text(
        encoding="utf-8"
    ) == "workspace data intact"

    # 4. Bootstrap plan inspection (cancelled before download/install, equivalent to dry-run plan check).
    monkeypatch.delenv("KINAIYA_INSTALL_ROOT", raising=False)
    bootstrap = _load_bootstrap_module()
    monkeypatch.setattr(bootstrap, "preflight", lambda **kw: None)
    monkeypatch.setattr(bootstrap, "missing_dependencies", lambda: ["bubblewrap"])
    monkeypatch.setattr(bootstrap, "resolve", lambda channel: (release, release["artifacts"][0]))
    bootstrap_home = tmp_path / "bootstrap-home"
    bootstrap_home.mkdir()
    monkeypatch.setattr(bootstrap.Path, "home", lambda: bootstrap_home)
    monkeypatch.setattr(
        bootstrap,
        "install_missing_dependencies",
        lambda *args: pytest.fail("declined plan must not install dependencies"),
    )
    monkeypatch.setattr(
        bootstrap, "download", lambda *args: pytest.fail("declined plan must not download")
    )
    decline_tty = _ControlledTTY(["1", "no"])

    @contextmanager
    def _decline_tty():
        yield decline_tty

    monkeypatch.setattr(bootstrap, "open_terminal", _decline_tty)
    plan_stdout = io.StringIO()
    with (
        redirect_stdout(plan_stdout),
        pytest.raises(bootstrap.BootstrapError, match="cancelled; no files changed"),
    ):
        bootstrap.run(["--channel", "nightly"], invoking_dir=tmp_path)
    assert "Choose install location [1]: " in decline_tty.output
    assert "Review the plan. Download and install? (y/n) [n]: " in decline_tty.output
    assert not list(bootstrap_home.iterdir())
    plan_json = json.loads(plan_stdout.getvalue().strip())
    assert plan_json["channel"] == "nightly"
    assert plan_json["source_sha"] == release["source_sha"]
    assert plan_json["missing_system_dependencies"] == ["bubblewrap"]
    assert (
        plan_json["system_dependency_command"]
        == "sudo apt-get install --no-upgrade --no-remove -o APT::Get::Upgrade=false -o APT::Get::Remove=false -o APT::Get::Allow-Downgrades=false -y -- bubblewrap"
    )


async def test_recovery_journeys_preserve_config_workspace_and_journal(tmp_path, monkeypatch):
    """Recovery matrix: missing/wrong API key, interrupted install, missing web assets, and 401/403 owner auth."""
    home = tmp_path / "recovery-home"
    layout = installation.Layout.for_home(home)
    archive_no_web, release_no_web = _build_release_archive(
        tmp_path,
        build=1,
        channel="nightly",
        include_web_assets=False,
    )
    installation.install_release(
        layout,
        release_no_web,
        archive_no_web,
        prepare=lambda *a: None,
        busy=lambda: False,
    )

    # Configure cloud Meta adapter referencing KINAIYA_TEST_MISSING_KEY.
    monkeypatch.delenv("KINAIYA_TEST_MISSING_KEY", raising=False)
    installation.setup(
        layout,
        endpoint="https://api.meta.ai/v1",
        model="muse-spark-1.3-contributor",
        adapter="meta",
        allow_cloud=True,
        credential_env="KINAIYA_TEST_MISSING_KEY",
        context_tokens=32768,
    )
    config_snapshot = layout.config.read_bytes()
    (layout.workspace / "important.txt").write_text("do not lose owner work\n", encoding="utf-8")

    # 1a. Missing credential env var at runtime provider instantiation fails closed with actionable ProviderError.
    cfg = json.loads(layout.config.read_text(encoding="utf-8"))
    with pytest.raises((ProviderError, RuntimeError), match="Meta credential is unavailable"):
        launcher.provider(cfg, "agent")
    assert layout.config.read_bytes() == config_snapshot
    assert (layout.workspace / "important.txt").read_text(
        encoding="utf-8"
    ) == "do not lose owner work\n"

    # 1b. Wrong API key / upstream 401 rejection surfaces ProviderError / FAILED task while preserving state.
    monkeypatch.setenv("KINAIYA_TEST_MISSING_KEY", "SYNTHETIC_WRONG_KEY_VALUE_DO_NOT_LOG")
    adapter = launcher.provider(cfg, "agent")
    assert isinstance(adapter, MetaResponsesProvider)
    await adapter.http.aclose()
    from packages.providers.complete_http import RequestBoundAsyncClient

    def _reject_401(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid bearer token"}})

    adapter.http = RequestBoundAsyncClient(
        base_url="https://api.meta.ai/v1/",
        timeout=30,
        headers={"Authorization": "Bearer SYNTHETIC_WRONG_KEY_VALUE_DO_NOT_LOG"},
        transport=httpx.MockTransport(_reject_401),
        trust_env=False,
        follow_redirects=False,
    )

    owner_file = layout.data / "owner-login.txt"
    with redirect_stdout(io.StringIO()):
        launcher.owner_login(SimpleNamespace(data_dir=layout.data, output=owner_file))
    valid_owner_token = owner_file.read_text(encoding="utf-8")

    async with _running_onboarding_stack(layout, adapter) as stack:
        # 2. Revoked / mismatched owner credential returns 401/403 while valid owner-login token succeeds.
        unauth_sess = await stack.session_http.post("/v1/sessions", json={"channel": "agent"})
        assert unauth_sess.status_code == 401

        revoked_headers = {"Authorization": "Bearer revoked-or-mismatched-owner-credential-token"}
        revoked_sess = await stack.session_http.post(
            "/v1/sessions",
            json={"channel": "agent"},
            headers=revoked_headers,
        )
        assert revoked_sess.status_code == 401

        revoked_task = await stack.agent_http.get("/v1/agent/tasks", headers=revoked_headers)
        assert revoked_task.status_code == 401

        # Non-owner service token (converse) is forbidden on owner-only Agent task listing.
        converse_headers = {"Authorization": f"Bearer {stack.tokens['converse']}"}
        forbidden_task = await stack.agent_http.get("/v1/agent/tasks", headers=converse_headers)
        assert forbidden_task.status_code == 403

        # Valid owner-login token succeeds and records session in SQLite journal.
        valid_headers = {"Authorization": f"Bearer {valid_owner_token}"}
        ok_sess = await stack.session_http.post(
            "/v1/sessions",
            json={"channel": "agent"},
            headers=valid_headers,
        )
        assert ok_sess.status_code == 200
        sess = ok_sess.json()

        # Submitting a task with the rejected 401 provider key fails the task cleanly without corrupting state.
        submit_resp = await stack.agent_http.post(
            "/v1/agent/tasks",
            json={
                "session_id": sess["session_id"],
                "thread_id": sess["thread_id"],
                "epoch": sess["epoch"],
                "objective": "Attempt task with invalid provider API key.",
                "content_scope": "public_synthetic",
                "idempotency_key": "recovery-invalid-key-1",
            },
            headers=valid_headers,
        )
        assert submit_resp.status_code == 201
        failed_task_id = submit_resp.json()["task_id"]
        failed_record = await asyncio.wait_for(
            stack.runtime.wait(failed_task_id, timeout=5.0), timeout=6.0
        )
        assert failed_record.state == TaskState.FAILED
        assert "provider error:" in (failed_record.error or "")
        assert "SYNTHETIC_WRONG_KEY_VALUE_DO_NOT_LOG" not in (failed_record.error or "")

    # 3. Missing Flutter web assets fails closed with actionable remediation without corrupting state.
    with pytest.raises(
        installation.InstallError, match="flutter build web in apps/kinaiya_flutter"
    ):
        web.serve_web(layout)

    # 4. Interrupted staging transaction refuses update until explicit `recover` reconciles it.
    abandoned_gen = layout.install / "generations" / ("b" * 32)
    abandoned_gen.mkdir(parents=True)
    (abandoned_gen / "partial.txt").write_text("interrupted stage", encoding="utf-8")
    installation.atomic_json(
        layout.install / "pending.json",
        {"generation": abandoned_gen.name, "phase": "staging"},
    )
    archive_v2, release_v2 = _build_release_archive(
        tmp_path, build=2, channel="nightly", include_web_assets=True
    )
    with pytest.raises(installation.InstallError, match="interrupted operation"):
        installation.install_release(
            layout,
            release_v2,
            archive_v2,
            prepare=lambda *a: None,
            busy=lambda: False,
        )
    recovery_receipt = installation.recover(layout, busy=lambda: False)
    assert recovery_receipt["action"] == "discarded_stage"
    assert recovery_receipt["database_restored"] is False
    assert not abandoned_gen.exists()

    # After recovery, updating to build 2 (which includes prebuilt web assets) succeeds.
    installation.install_release(
        layout,
        release_v2,
        archive_v2,
        prepare=lambda *a: None,
        busy=lambda: False,
    )
    current_rec = installation.current_receipt(layout)
    assert current_rec is not None
    active_source = (layout.install / "current").resolve() / current_rec["source"]
    assert web.web_root(active_source).is_dir()

    # Verify config, workspace, and SQLite journal were preserved across all failure & recovery paths.
    assert layout.config.read_bytes() == config_snapshot
    assert (layout.workspace / "important.txt").read_text(
        encoding="utf-8"
    ) == "do not lose owner work\n"
    verify_store = Store(layout.data / "journal.sqlite")
    try:
        outcomes = verify_store.read(kinds=["agent.outcome"])
        assert len(outcomes) == 1
    finally:
        verify_store.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits and Linux sandbox probe")
def test_evidence_redaction_and_real_vs_mocked_qualification_boundary(tmp_path, monkeypatch):
    """Verify zero secret leakage in receipts/config/logs and record mocked vs real Linux qualification evidence."""
    secret_api_key = "sk-kinaiya-qualification-secret-do-not-leak-99999"
    monkeypatch.setenv("KINAIYA_QUAL_SECRET_KEY", secret_api_key)

    home = tmp_path / "evidence-home"
    layout = installation.Layout.for_home(home)
    archive, release = _build_release_archive(tmp_path, build=3, channel="nightly")

    installation.install_release(
        layout,
        release,
        archive,
        prepare=lambda *a: None,
        busy=lambda: False,
    )
    installation.setup(
        layout,
        endpoint="https://api.meta.ai/v1",
        model="muse-spark-1.3-contributor",
        adapter="meta",
        allow_cloud=True,
        credential_env="KINAIYA_QUAL_SECRET_KEY",
        context_tokens=32768,
    )

    owner_out = io.StringIO()
    owner_login_file = layout.data / "owner-login.txt"
    with redirect_stdout(owner_out):
        launcher.owner_login(SimpleNamespace(data_dir=layout.data, output=owner_login_file))

    monkeypatch.setenv("KINAIYA_INSTALL_ROOT", str(layout.install))
    version_info = dist_cli.dispatch(SimpleNamespace(command="version"))
    receipt = installation.current_receipt(layout)
    assert receipt is not None

    tokens = load_or_create_tokens(layout.data)
    assert stat.S_IMODE(layout.config.stat().st_mode) == 0o600
    assert stat.S_IMODE((layout.data / "service-tokens.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(owner_login_file.stat().st_mode) == 0o600

    # Exercise real bwrap probe if available on the Linux host to record truthful sandbox capability.
    from services.agent.tools import bwrap_path

    real_bwrap = bwrap_path()
    bwrap_probe_passed = False
    if real_bwrap is not None:
        import subprocess

        probe = subprocess.run(
            [
                real_bwrap,
                "--unshare-all",
                "--die-with-parent",
                "--new-session",
                "--ro-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--clearenv",
                "/usr/bin/true",
            ],
            capture_output=True,
            timeout=15,
        )
        bwrap_probe_passed = probe.returncode == 0

    qualification_evidence = {
        "issue": 398,
        "release": {
            "channel": receipt["release"]["channel"],
            "version": receipt["release"]["version"],
            "build_number": receipt["release"]["build_number"],
            "source_sha": receipt["release"]["source_sha"],
        },
        "version_command": version_info,
        "owner_login_stdout": json.loads(owner_out.getvalue()),
        "sandbox": {
            "bwrap_binary_present": real_bwrap is not None,
            "bwrap_namespace_probe_passed": bwrap_probe_passed,
        },
        "mocked_transports": [
            "github_release_feed_and_tarball_download",
            "apt_get_system_dependency_installer",
            "pip_wheel_network_install_in_prepare_runtime",
            "llm_provider_wire_transport",
        ],
        "real_exercised_components": [
            "tar_gzip_streaming_extractor_and_sha256_verification",
            "atomic_symlink_generation_switch_and_flock_operation_lock",
            "interactive_tty_wizard_and_noninteractive_setup",
            "posix_0600_service_tokens_and_owner_login_export",
            "fastapi_asgi_session_and_agent_services_with_sqlite_journal",
        ],
    }
    serialized_evidence = json.dumps(qualification_evidence, indent=2)

    # Redaction assertions: neither raw API keys nor any service/owner bearer token may appear
    # in config.json, receipt.json, layout.json, CLI stdout, or qualification evidence logs.
    inspected_surfaces = {
        "config.json": layout.config.read_text(encoding="utf-8"),
        "receipt.json": ((layout.install / "current") / "receipt.json").read_text(encoding="utf-8"),
        "layout.json": (layout.install / "layout.json").read_text(encoding="utf-8"),
        "owner_login_stdout": owner_out.getvalue(),
        "qualification_evidence": serialized_evidence,
    }
    forbidden_secrets = [secret_api_key, *tokens.values()]
    for surface_name, content in inspected_surfaces.items():
        for secret_val in forbidden_secrets:
            assert secret_val not in content, f"Secret leaked into {surface_name}"

    # Only the explicitly requested 0600 owner-login.txt file contains the owner token.
    assert owner_login_file.read_text(encoding="utf-8") == tokens["owner"]
