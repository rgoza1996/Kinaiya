"""Owner voice configuration and synthetic managed-health receipts; no model calls."""

import os
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from services.common.auth import Auth
from services.resources import launcher

REVISION = "aafc6e32416a594460b32413efc49d7fe4ce6d46"
VIBEVOICE_REVISION = "6bce5f06044837fe6d2c5d7a71a84f0416bd57e4"
MISSING = object()


@pytest.fixture
async def voice_host(tmp_path, monkeypatch):
    clients, apps, managers = [], [], []
    health = {}
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200, json=health))
        result = original_client(*args, **kwargs)
        clients.append(result)
        return result

    class Manager:
        def __init__(self, specs, readiness):
            self.specs, self.readiness = specs, readiness
            managers.append(self)

        def status(self):
            return {"workers": {"voice": {"state": "stopped", "ready": False}}}

        async def close(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(launcher, "WorkerManager", Manager)

    def build(profile=MISSING, **resource_changes):
        voice = {"url": "http://127.0.0.1:8915"}
        if profile is not MISSING:
            voice["profile"] = profile
        config = {
            "session_url": "http://127.0.0.1:8910",
            "agent_url": "http://127.0.0.1:8911",
            "converse_url": "http://127.0.0.1:8912",
            "voice": voice,
            "resources": {
                "enabled": True,
                "voice_python": sys.executable,
                "voice_port": 8915,
                **resource_changes,
            },
        }
        args = SimpleNamespace(
            data_dir=tmp_path / f"data-{len(apps)}", workspace=tmp_path / "agent"
        )
        tokens = {"owner": "o" * 32, "resources": "r" * 32}
        app = launcher.resource_app(args, config, Auth(tokens), tokens, Path.cwd())
        apps.append(app)
        return app, managers[-1]

    yield build, health
    for app in apps:
        await app.state.coordinator.close()
    for connection in clients:
        await connection.aclose()


@pytest.mark.parametrize(
    "profile,voice,expected_voice,mode,revision",
    [
        (MISSING, None, "af_heart", "sentence_buffered", None),
        ("Kokoro 82M", "af_heart", "af_heart", "sentence_buffered", None),
        ("Supertonic 3", None, "F1", "buffered", REVISION),
        ("Supertonic 3", "M3", "M3", "buffered", REVISION),
        ("VibeVoice Realtime 0.5B", None, "en-Carter_man", "native_streaming", VIBEVOICE_REVISION),
    ],
)
async def test_managed_voice_cli_and_health_match_owner_selection(
    voice_host, tmp_path, profile, voice, expected_voice, mode, revision
):
    build, health = voice_host
    pinned = profile in ("Supertonic 3", "VibeVoice Realtime 0.5B")
    model_dir = str(tmp_path / "checkpoint") if pinned else None
    app, manager = build(profile, tts_model_dir=model_dir, tts_voice=voice)
    assert len(manager.specs) == 1
    spec = manager.specs[0]
    expected_profile = "Kokoro 82M" if profile is MISSING else profile
    args = spec.argv
    assert spec.id == "voice"
    assert args[:3] == (sys.executable, "-m", "packages.providers.voice_worker")
    assert args[args.index("--tts-profile") + 1] == expected_profile
    assert args[args.index("--tts-voice") + 1] == expected_voice
    if model_dir is None:
        assert "--tts-model-dir" not in args
    else:
        assert args[args.index("--tts-model-dir") + 1] == model_dir
    assert spec.env["HF_HUB_OFFLINE"] == "1"
    assert spec.env["TRANSFORMERS_OFFLINE"] == "1"
    assert "--resource-policy" in args
    health.update(
        service="local-voice",
        pid=42,
        device="cpu",
        loaded=[],
        tts_profile=expected_profile,
        tts_voice=expected_voice,
        synthesis_mode=mode,
        tts_artifact_revision=revision,
        assets_state="not_checked",
    )
    assert await manager.readiness(spec, SimpleNamespace(pid=42))
    assert app.state.voice_health == {
        key: value for key, value in health.items() if key != "service"
    }
    health["loaded"] = ["stt", "tts"]
    if pinned:
        health["assets_state"] = "verified"
    assert await manager.readiness(spec, SimpleNamespace(pid=42))


@pytest.mark.parametrize(
    "changed,value",
    [
        ("tts_profile", "Kokoro 82M"),
        ("tts_voice", "af_heart"),
        ("tts_artifact_revision", None),
        ("tts_artifact_revision", "b" * 40),
        ("synthesis_mode", "sentence_buffered"),
        ("assets_state", "ready"),
        ("loaded", ["fake-model"]),
        ("loaded", ["tts", "tts"]),
        ("loaded", {"tts": True}),
        ("loaded", [None]),
        ("pid", "42"),
        ("pid", 999),
        ("device", "cuda"),
        ("service", "unowned-voice"),
    ],
)
async def test_managed_voice_rejects_mismatched_health(voice_host, tmp_path, changed, value):
    build, health = voice_host
    app, manager = build("Supertonic 3", tts_model_dir=str(tmp_path / "checkpoint"))
    health.update(
        service="local-voice",
        pid=42,
        device="cpu",
        loaded=[],
        tts_profile="Supertonic 3",
        tts_voice="F1",
        synthesis_mode="buffered",
        tts_artifact_revision=REVISION,
        assets_state="not_checked",
    )
    health[changed] = value
    assert not await manager.readiness(manager.specs[0], SimpleNamespace(pid=42))
    assert app.state.voice_health == {}


async def test_later_profile_mismatch_clears_a_previous_health_receipt(voice_host):
    build, health = voice_host
    app, manager = build()
    health.update(
        service="local-voice",
        pid=42,
        device="cpu",
        loaded=[],
        tts_profile="Kokoro 82M",
        tts_voice="af_heart",
        synthesis_mode="sentence_buffered",
        tts_artifact_revision=None,
        assets_state="not_checked",
    )
    assert await manager.readiness(manager.specs[0], SimpleNamespace(pid=42))
    assert app.state.voice_health["tts_profile"] == "Kokoro 82M"
    health["tts_profile"] = "Supertonic 3"
    assert not await manager.readiness(manager.specs[0], SimpleNamespace(pid=42))
    assert app.state.voice_health == {}


async def test_managed_supertonic_cannot_claim_loaded_before_artifact_verification(
    voice_host, tmp_path
):
    build, health = voice_host
    app, manager = build("Supertonic 3", tts_model_dir=str(tmp_path / "checkpoint"))
    health.update(
        service="local-voice",
        pid=42,
        device="cpu",
        loaded=["tts"],
        tts_profile="Supertonic 3",
        tts_voice="F1",
        synthesis_mode="buffered",
        tts_artifact_revision=REVISION,
        assets_state="not_checked",
    )
    assert not await manager.readiness(manager.specs[0], SimpleNamespace(pid=42))
    assert app.state.voice_health == {}


@pytest.mark.parametrize(
    "field", ["tts_profile", "tts_voice", "synthesis_mode", "tts_artifact_revision", "assets_state"]
)
async def test_managed_voice_requires_complete_health_metadata(voice_host, field):
    build, health = voice_host
    app, manager = build()
    health.update(
        service="local-voice",
        pid=42,
        device="cpu",
        loaded=[],
        tts_profile="Kokoro 82M",
        tts_voice="af_heart",
        synthesis_mode="sentence_buffered",
        tts_artifact_revision=None,
        assets_state="not_checked",
    )
    health.pop(field)
    assert not await manager.readiness(manager.specs[0], SimpleNamespace(pid=42))
    assert app.state.voice_health == {}


@pytest.mark.parametrize(
    "profile,settings",
    [
        ("Supertonic 3", {}),
        ("Kokoro 82M", {"tts_model_dir": "/owned/checkpoint"}),
        ("Kokoro 82M", {"tts_voice": "F1"}),
        ("Supertonic 3", {"tts_model_dir": "/owned/checkpoint", "tts_voice": "af_heart"}),
        ("VibeVoice Realtime 0.5B", {"tts_model_dir": "/owned/checkpoint", "tts_voice": "F1"}),
        ("Kokoro 82M", {"tts_voice": "en-Carter_man"}),
        ("Ming 3 Tiny", {}),
        ("VibeVoice Realtime 0.5B", {}),
        ("unconfigured", {}),
        (None, {}),
        ([], {}),
    ],
)
async def test_invalid_or_unconfigured_profile_fails_before_worker_construction(
    voice_host, profile, settings
):
    build, _ = voice_host
    with pytest.raises(ValueError):
        build(profile, **settings)


@pytest.mark.parametrize("placement", ["same", "inside", "ancestor", "symlink_into", "symlink_out"])
@pytest.mark.parametrize("profile", ["Supertonic 3", "VibeVoice Realtime 0.5B"])
async def test_managed_models_cannot_overlap_agent_writable_inputs(
    voice_host, tmp_path, placement, profile
):
    build, _ = voice_host
    workspace = tmp_path / "agent"
    if placement == "same":
        model_dir = workspace
    elif placement == "inside":
        model_dir = workspace / "weights"
    elif placement == "ancestor":
        model_dir = tmp_path
    elif placement == "symlink_into":
        target = workspace / "weights"
        target.mkdir(parents=True)
        model_dir = tmp_path / "cache-link"
        model_dir.symlink_to(target, target_is_directory=True)
    else:
        workspace.mkdir()
        target = tmp_path / "external-cache"
        target.mkdir()
        model_dir = workspace / "cache-link"
        model_dir.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the Agent workspace"):
        build(profile, tts_model_dir=str(model_dir))


@pytest.mark.parametrize(
    "changes",
    [
        {"tts_model_dir": "relative/checkpoint"},
        {"tts_model_dir": "~/checkpoint"},
        {"tts_model_dir": ""},
        {"tts_model_dir": "/checkpoint\x00suffix"},
        {"tts_model_dir": "/checkpoint\n--other-option"},
        {"tts_model_dir": True},
        {"tts_voice": "F6"},
        {"tts_voice": "../../voice"},
        {"tts_voice": 1},
        {"tts_profile": "Supertonic 3"},
        {"tts_argv": ["arbitrary"]},
    ],
)
def test_voice_settings_remain_strict_owner_inputs(changes):
    with pytest.raises(ValidationError):
        launcher.LocalResourceConfig.model_validate(
            {"enabled": True, "voice_python": sys.executable, **changes}
        )


def test_supertonic_dependencies_are_opt_in_and_pinned():
    project = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    assert project["project"]["optional-dependencies"]["supertonic"] == [
        "supertonic==1.3.1",
        "onnxruntime==1.23.1",
        "numpy==2.3.3",
        "soundfile==0.13.1",
    ]
    assert set(project["tool"]["setuptools"]["package-data"]["packages.providers"]) == {
        "supertonic3_manifest.json",
        "vibevoice_manifest.json",
        "whistle_manifest.json",
        "whistle_LICENSE.txt",
        "whistle_NOTICE.txt",
        "qwen3_manifest.json",
        "qwen3_LICENSE.txt",
    }
    assert not any(
        name in item
        for item in project["project"]["dependencies"]
        for name in ("supertonic", "onnxruntime", "kokoro", "torch")
    )


def test_independent_agent_starts_with_optional_voice_imports_blocked(tmp_path):
    # A disposable interpreter gives the import guard fresh modules. It starts
    # no HTTP server, model worker, external provider or inference operation.
    script = textwrap.dedent("""
        import asyncio
        import importlib.abc
        import sys
        from pathlib import Path
        from types import SimpleNamespace

        forbidden = {'vibevoice', 'diffusers', 'accelerate', 'supertonic', 'onnxruntime', 'kokoro', 'torch', 'numpy',
                     'soundfile', 'faster_whisper', 'transformers', 'huggingface_hub'}
        class NoVoice(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split('.')[0] in forbidden:
                    raise AssertionError('optional voice import: ' + fullname)
        sys.meta_path.insert(0, NoVoice())
        import httpx
        from core.policy import ToolPolicy
        from packages.contracts.models import ProviderInfo
        from services.agent.app import create_app
        from services.agent.runtime import AgentRuntime
        from services.common.auth import Auth
        from services.resources.launcher import LocalResourceConfig

        workspace = Path(sys.argv[1])
        provider = SimpleNamespace(info=ProviderInfo(provider='synthetic', model='none', local=True, streaming=False))
        runtime = AgentRuntime(None, provider, workspace, ToolPolicy(workspace))
        app = create_app(runtime, Auth({'owner': 'o' * 32}))
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture') as client:
                response = await client.get('/health')
                assert response.status_code == 200
                assert response.json()['service'] == 'agent'
                assert response.json()['status'] == 'ready'
            await runtime.close()
        asyncio.run(check())
        assert not (forbidden & sys.modules.keys())
    """)
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=True,
        timeout=15,
        capture_output=True,
        env={"PATH": os.defpath, "PYTHONPATH": str(Path(__file__).parents[2])},
    )
