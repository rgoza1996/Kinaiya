"""Synthetic configuration/readiness checks; no model or GPU is loaded."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.resources import laya as module
from services.resources.laya import ManagedLayaConfig, laya_gpu_sample, laya_ready, laya_worker

BDF = "0000:03:00.0"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    model = tmp_path / "model"
    model.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    workspace = tmp_path / "agent"
    workspace.mkdir()
    settings = ManagedLayaConfig(
        python=sys.executable, model_dir=str(model), manifest=str(manifest), port=8879
    )
    profile = {
        "kind": "laya",
        "base_url": "http://127.0.0.1:8879",
        "model": "synthetic",
        "expected_artifact_sha256": "a" * 64,
        "api_key_env": "SYNTHETIC_LAYA_KEY",
    }
    monkeypatch.setenv("SYNTHETIC_LAYA_KEY", "public-fixture-secret")
    monkeypatch.setenv("META_API_KEY", "must-not-be-forwarded")
    return settings, profile, workspace


async def test_fixed_first_party_spec_excludes_parent_credentials(configured):
    settings, profile, workspace = configured
    spec, probe = laya_worker(settings, profile, Path.cwd(), workspace)
    try:
        assert spec.id == "laya" and spec.argv[:3] == (
            sys.executable,
            "-m",
            "services.decisions.laya_worker",
        )
        assert "public-fixture-secret" not in spec.argv
        assert spec.env[module.CHILD_SECRET_ENV] == "public-fixture-secret"
        assert "SYNTHETIC_LAYA_KEY" not in spec.env
        assert "META_API_KEY" not in spec.env
        assert spec.env["HF_HUB_OFFLINE"] == "1"
        assert probe.expected_artifact_sha256 == "a" * 64 and probe.info.local
    finally:
        await probe.close()


def test_laya_cannot_reuse_an_owner_or_service_token(configured):
    settings, profile, workspace = configured
    with pytest.raises(ValueError, match="must not reuse"):
        laya_worker(
            settings, profile, Path.cwd(), workspace, service_tokens=("public-fixture-secret",)
        )


async def test_explicit_gpu_selection_must_also_match_kernel_bus_id(configured):
    settings, profile, workspace = configured
    settings = settings.model_copy(update={"device": "cuda", "pci_bus_id": BDF, "gpu_index": 1})
    spec, probe = laya_worker(settings, profile, Path.cwd(), workspace)
    try:
        assert spec.env["ROCR_VISIBLE_DEVICES"] == "1"
        assert spec.argv[spec.argv.index("--device") + 1] == "cuda"
    finally:
        await probe.close()


@pytest.mark.parametrize(
    "fields",
    [
        {"device": "cuda"},
        {"pci_bus_id": BDF},
        {"gpu_index": True},
        {"device": "cuda", "pci_bus_id": "../card"},
        {"argv": ["bad"]},
    ],
)
def test_configuration_cannot_claim_unbound_gpu_or_arbitrary_command(configured, fields):
    settings, _, _ = configured
    with pytest.raises(ValueError):
        ManagedLayaConfig.model_validate({**settings.model_dump(), **fields})


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "compatible"},
        {"base_url": "https://example.invalid"},
        {"allow_cloud": True},
        {"expected_artifact_sha256": None},
        {"model": "../remote"},
        {"api_key_env": None},
        {"api_key": "do-not-embed"},
    ],
)
def test_provider_must_match_pinned_separate_local_secret(configured, change):
    settings, profile, workspace = configured
    with pytest.raises(ValueError):
        laya_worker(settings, {**profile, **change}, Path.cwd(), workspace)


@pytest.mark.parametrize("kind", ["inside", "ancestor", "relative"])
def test_owner_inputs_disjoint_from_agent_workspace(configured, tmp_path, kind):
    settings, profile, workspace = configured
    path = workspace if kind == "inside" else tmp_path if kind == "ancestor" else Path("relative")
    settings = settings.model_copy(update={"model_dir": str(path)})
    with pytest.raises(ValueError):
        laya_worker(settings, profile, Path.cwd(), workspace)


def test_workspace_symlink_cannot_become_a_mutable_worker_executable(configured):
    settings, profile, workspace = configured
    executable = workspace / "python"
    executable.symlink_to(Path(sys.executable).resolve())
    settings = settings.model_copy(update={"python": str(executable)})
    with pytest.raises(ValueError, match="outside the Agent workspace"):
        laya_worker(settings, profile, Path.cwd(), workspace)


async def test_secret_reference_cannot_replace_the_trusted_child_pythonpath(
    configured, monkeypatch
):
    settings, profile, workspace = configured
    monkeypatch.setenv("PYTHONPATH", "public-fixture-secret")
    spec, probe = laya_worker(
        settings, {**profile, "api_key_env": "PYTHONPATH"}, Path.cwd(), workspace
    )
    try:
        assert spec.env["PYTHONPATH"] == str(Path.cwd())
        assert spec.env[module.CHILD_SECRET_ENV] == "public-fixture-secret"
        assert spec.argv[spec.argv.index("--api-key-env") + 1] == module.CHILD_SECRET_ENV
    finally:
        await probe.close()


@pytest.mark.parametrize(
    "health",
    [
        {"ready": False},
        {"ready": True, "worker_pid": 999, "device": "cpu"},
        {"ready": True, "device": "cpu"},
        {"ready": True, "worker_pid": 42, "device": "cuda"},
    ],
)
async def test_readiness_cannot_adopt_foreign_or_wrong_device_process(configured, health):
    async def probe():
        return health

    settings, _, _ = configured
    assert not await laya_ready(settings, SimpleNamespace(health=probe), SimpleNamespace(pid=42))


async def test_gpu_readiness_requires_actual_configured_device_residency(configured, monkeypatch):
    async def probe():
        return {"ready": True, "worker_pid": 42, "device": "cuda:0"}

    settings, _, _ = configured
    settings = settings.model_copy(update={"device": "cuda", "pci_bus_id": BDF})
    monkeypatch.setattr(module, "read_amd_memory", lambda bdf: {"total_bytes": 100})
    for resident in [None, 0, 101, 50]:
        monkeypatch.setattr(module, "read_amd_process_vram", lambda pid, bdf, value=resident: value)
        assert await laya_ready(
            settings, SimpleNamespace(health=probe), SimpleNamespace(pid=42)
        ) is (resident == 50)


@pytest.mark.parametrize(
    "state,ready,rss,evidence,expected",
    [
        ("stopped", False, None, None, None),
        ("stopped", False, None, {"reaped": True, "group_empty": True}, 0),
        ("stopped", False, None, {"reaped": True, "group_empty": False}, None),
        ("running", True, 100, None, 50),
        ("running", True, None, None, None),
        ("running", False, 100, None, None),
        ("failed", False, None, None, None),
    ],
)
def test_gpu_sampler_never_invents_release_from_lost_ownership(
    configured, monkeypatch, state, ready, rss, evidence, expected
):
    settings, _, _ = configured
    settings = settings.model_copy(update={"device": "cuda", "pci_bus_id": BDF})
    monkeypatch.setattr(
        module,
        "read_amd_memory",
        lambda _: {"total_bytes": 100, "used_bytes": 60, "free_bytes": 40},
    )
    monkeypatch.setattr(module, "read_amd_process_vram", lambda pid, bdf: 50)
    manager = SimpleNamespace(
        status=lambda: {
            "workers": {
                "laya": {
                    "state": state,
                    "ready": ready,
                    "owned_pid": 42,
                    "rss_bytes": rss,
                    "stop_evidence": evidence,
                }
            }
        }
    )
    value = laya_gpu_sample(settings, manager)
    assert (value["worker_resident_bytes"] if value is not None else None) == expected
