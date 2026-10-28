"""Filesystem and process-boundary checks with tiny synthetic model bytes."""

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from packages.providers.laya_artifact import (
    ARTIFACT_HEADER,
    WORKER_PID_HEADER,
    ArtifactError,
    verified_laya_checkpoint,
)
from services.decisions import laya_worker

NAMES = [
    "model.safetensors",
    "rl_agent_config.json",
    "encoder/config.json",
    "tokenizer/tokenizer.json",
    "tokenizer/tokenizer_config.json",
]


@pytest.fixture
def artifact(tmp_path):
    model = tmp_path / "model"
    entries = []
    for name in NAMES:
        path = model / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = ("synthetic " + name).encode()
        path.write_bytes(data)
        entries.append(
            {"path": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    manifest = tmp_path / "manifest.json"
    contents = {"model_alias": "public-synthetic", "files": entries}

    def freeze(raw=None):
        manifest.write_bytes(raw if raw is not None else json.dumps(contents).encode())
        return model, manifest, hashlib.sha256(manifest.read_bytes()).hexdigest()

    return SimpleNamespace(model=model, manifest=manifest, contents=contents, freeze=freeze)


def test_owned_snapshot_survives_source_replacement_and_cleans_only_itself(artifact):
    source, manifest, pin = artifact.freeze()
    original = (source / NAMES[0]).read_bytes()
    with verified_laya_checkpoint(source, manifest, pin) as verified:
        owned = verified.directory
        assert owned != source
        assert verified.model_alias == "public-synthetic" and verified.sha256 == pin
        assert stat.S_IMODE(owned.stat().st_mode) == 0o700
        assert stat.S_IMODE((owned / NAMES[0]).stat().st_mode) == 0o400
        (source / NAMES[0]).unlink()
        (source / NAMES[0]).write_bytes(b"untrusted replacement")
        assert (owned / NAMES[0]).read_bytes() == original
        with pytest.raises(TypeError):
            verified.digests[NAMES[0]] = "0" * 64
    assert not owned.exists()
    assert source.exists() and (source / NAMES[0]).read_bytes() == b"untrusted replacement"


def test_snapshot_cleanup_on_loading_failure(artifact):
    with pytest.raises(RuntimeError), verified_laya_checkpoint(*artifact.freeze()) as verified:
        owned = verified.directory
        raise RuntimeError("synthetic loader failure")
    assert not owned.exists() and artifact.model.exists()


@pytest.mark.parametrize("pin", [None, "", "0" * 63, "A" * 64, 1, True])
def test_invalid_owner_pin_rejected(artifact, pin):
    model, manifest, _ = artifact.freeze()
    with pytest.raises(ArtifactError), verified_laya_checkpoint(model, manifest, pin):
        pytest.fail("invalid pin was accepted")


def test_manifest_bytes_must_match_separately_configured_pin(artifact):
    model, manifest, pin = artifact.freeze()
    manifest.write_bytes(manifest.read_bytes() + b" ")
    with pytest.raises(ArtifactError, match="manifest digest mismatch"):
        with verified_laya_checkpoint(model, manifest, pin):
            pytest.fail("changed manifest was accepted")


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m["files"].pop(),
        lambda m: m["files"].append(dict(m["files"][0])),
        lambda m: m["files"][0].update(path="../outside"),
        lambda m: m["files"][0].update(path="/outside"),
        lambda m: m["files"][0].update(path="encoder/../config.json"),
        lambda m: m["files"][1].update(path=m["files"][0]["path"]),
        lambda m: m["files"][0].update(size_bytes=True),
        lambda m: m["files"][0].update(size_bytes=0),
        lambda m: m["files"][0].update(size_bytes=2 * 1024**3 + 1),
        lambda m: m["files"][1].update(size_bytes=16 * 1024**2 + 1),
        lambda m: m["files"][0].update(sha256="not a digest"),
        lambda m: m["files"][0].update(unexpected="field"),
        lambda m: m.update(model_alias="auto/remote"),
    ],
)
def test_manifest_structure_rejected_before_copy(artifact, change):
    change(artifact.contents)
    with pytest.raises(ArtifactError), verified_laya_checkpoint(*artifact.freeze()):
        pytest.fail("malformed manifest was accepted")


@pytest.mark.parametrize("raw", [b"[]", b"null", b"{", b'"' + b"x" * 65536 + b'"'])
def test_bounded_manifest_json(artifact, raw):
    with pytest.raises(ArtifactError), verified_laya_checkpoint(*artifact.freeze(raw)):
        pytest.fail("malformed manifest was accepted")


def test_duplicate_json_keys_refused(artifact):
    raw = json.dumps(artifact.contents).replace(
        '"model_alias": "public-synthetic"',
        '"model_alias": "public-synthetic", "model_alias": "replacement"',
    )
    with pytest.raises(ArtifactError), verified_laya_checkpoint(*artifact.freeze(raw.encode())):
        pytest.fail("duplicate keys were accepted")


@pytest.mark.parametrize("kind", ["file", "intermediate", "root", "manifest", "fifo"])
def test_no_follow_or_special_file_open(artifact, tmp_path, kind):
    model, manifest, pin = artifact.freeze()
    if kind == "file":
        original = model / NAMES[0]
        original.rename(tmp_path / "outside")
        original.symlink_to(tmp_path / "outside")
    elif kind == "intermediate":
        (model / "encoder").rename(tmp_path / "outside")
        (model / "encoder").symlink_to(tmp_path / "outside", target_is_directory=True)
    elif kind == "root":
        link = tmp_path / "link"
        link.symlink_to(model, target_is_directory=True)
        model = link
    elif kind == "manifest":
        manifest.rename(tmp_path / "outside")
        manifest.symlink_to(tmp_path / "outside")
    else:
        (model / NAMES[0]).unlink()
        os.mkfifo(model / NAMES[0])
    with pytest.raises(ArtifactError), verified_laya_checkpoint(model, manifest, pin):
        pytest.fail("unsafe filesystem entry was accepted")


@pytest.mark.parametrize("replacement", [b"x", b"X" * 27])
def test_size_or_same_size_digest_tampering_refused(artifact, replacement):
    args = artifact.freeze()
    original = artifact.model / NAMES[0]
    if len(replacement) > 1:
        replacement = b"X" * original.stat().st_size
    original.write_bytes(replacement)
    with pytest.raises(ArtifactError), verified_laya_checkpoint(*args):
        pytest.fail("tampered weights were accepted")


def test_worker_import_does_not_load_model_frameworks():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import services.decisions.laya_worker; "
            "assert 'torch' not in sys.modules and 'laya' not in sys.modules",
        ],
        check=True,
        timeout=10,
        env={**os.environ, "PYTHONPATH": str(Path.cwd())},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,status,has_identity", [("test", 200, True), ("bad", 200, False), ("test", 503, False)]
)
async def test_identity_header_only_on_authenticated_success(key, status, has_identity):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    bound = laya_worker.ArtifactIdentityApp(app, "a" * 64, "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bound)) as client:
        response = await client.get(
            "http://worker/health", headers={"Authorization": f"Bearer {key}"}
        )
    assert (response.headers.get(ARTIFACT_HEADER) == "a" * 64) is has_identity
    assert (response.headers.get(WORKER_PID_HEADER) == str(os.getpid())) is has_identity


@pytest.mark.parametrize("mutate_on_load", [False, True])
def test_worker_limits_registry_offline_and_detects_load_mutation(
    artifact, monkeypatch, mutate_on_load
):
    made = []

    class Router:
        def __init__(self, **kwargs):
            self.options = kwargs
            self.models = {"english": "remote", **kwargs["models"]}
            self.unloaded = False
            made.append(self)

        def load(self, name):
            assert set(self.models) == {name}
            assert self.options["auto_task_detection"] is False
            assert self.options["max_loaded"] == 1
            if mutate_on_load:
                path = Path(self.models[name]) / "tokenizer/tokenizer_config.json"
                path.chmod(0o600)
                path.write_text("normalized behind the pin")

        def unload(self):
            self.unloaded = True

        def predict(self, *args, **kwargs):
            return kwargs["model"]

        predict_batch = predict

    monkeypatch.setattr(laya_worker, "version", lambda _: "0.4.16")
    monkeypatch.setattr(os, "environ", {"LAYA_EXTRA_MODELS": "remote", "HF_TOKEN": "synthetic"})
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(set_num_threads=lambda n: None))
    monkeypatch.setitem(sys.modules, "laya.router", SimpleNamespace(Router=Router))
    monkeypatch.setitem(
        sys.modules, "laya.serve", SimpleNamespace(create_app=lambda router: object())
    )
    with verified_laya_checkpoint(*artifact.freeze()) as checkpoint:
        if mutate_on_load:
            with pytest.raises(ArtifactError, match="changed during load"):
                laya_worker.build_app(
                    checkpoint, device="cpu", threads=4, concurrency=2, api_key="test"
                )
            assert made[0].unloaded
        else:
            app, router = laya_worker.build_app(
                checkpoint, device="cpu", threads=4, concurrency=2, api_key="test"
            )
            assert app.identity[1].decode() == checkpoint.sha256
            assert router.options["sha256_digests"][checkpoint.model_alias] == checkpoint.digests
            assert not router.unloaded
            for predict in [router.predict, router.predict_batch]:
                with pytest.raises(ValueError, match="Explicit pinned"):
                    predict("public synthetic", {}, model=None)
                assert (
                    predict("public synthetic", {}, model=checkpoint.model_alias)
                    == checkpoint.model_alias
                )
    assert os.environ["HF_HUB_OFFLINE"] == "1" and "HF_TOKEN" not in os.environ
    assert "LAYA_EXTRA_MODELS" not in os.environ
