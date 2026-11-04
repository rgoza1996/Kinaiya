"""Deterministic unit and worker integration tests for Ming 3 Tiny TTS (#72)."""

import copy
from dataclasses import dataclass
import hashlib
import io
import json
import struct
import sys
import threading
from types import SimpleNamespace
import wave
from pathlib import Path

import httpx
import pytest

import packages.providers.ming3_backend as ming3_mod
from packages.providers.ming3_backend import (
    MANIFEST,
    MING3_MAX_CHARS,
    MING3_MAX_SAMPLES,
    MING3_REVISION,
    MING3_RUNTIME_REVISION,
    MING3_SAMPLE_RATE,
    MING3_VOICE,
    Ming3TinyBackend,
    SynthesisCancelled,
    validate_ming3_request,
    verify_ming3_assets,
)
from packages.providers.tts_backends import (
    TTSBackendError,
    artifact_revision,
    default_voice,
    input_limit,
    synthesis_mode,
    validate_tts_device,
    validate_voice,
)
from packages.providers.voice_worker import create_app
from services.common.auth import Auth

AUTH = Auth({"owner": "o" * 32, "converse": "c" * 32})


def _build_safetensors_bytes(total_size: int = 512) -> bytes:
    header_obj = {
        "__metadata__": {"format": "pt"},
        "weight": {
            "dtype": "F32",
            "shape": [4, 4],
            "data_offsets": [0, 64],
        },
    }
    raw_header = json.dumps(header_obj, separators=(",", ":")).encode("utf-8")
    pad_len = (8 - (len(raw_header) % 8)) % 8
    raw_header += b" " * pad_len
    prefix = struct.pack("<Q", len(raw_header))
    body_len = total_size - 8 - len(raw_header)
    assert body_len >= 64
    body = b"\x00" * body_len
    return prefix + raw_header + body


def _create_synthetic_ming3_dir(root: Path) -> tuple[Path, dict]:
    model_dir = root / "ming3-checkpoint"
    model_dir.mkdir(parents=True)

    shard_1 = "model-00001-of-00002.safetensors"
    shard_2 = "model-00002-of-00002.safetensors"
    files = {
        "LICENSE": b"MIT License\nCopyright (c) 2025 inclusionAI\n",
        "LEGAL.md": b"Legal Disclaimer\n",
        "config.json": json.dumps(
            {
                "architectures": ["LingV3TTSForConditionalGeneration"],
                "model_type": "lingv3_tts",
                "auto_map": {
                    "AutoConfig": "configuration_lingv3_tts.LingV3TTSConfig",
                    "AutoModel": "modeling_lingv3_tts.LingV3TTSForConditionalGeneration",
                },
                "tts_config": {"sample_rate": MING3_SAMPLE_RATE},
            }
        ).encode("utf-8"),
        "generation_config.json": json.dumps({"max_new_tokens": 250}).encode("utf-8"),
        "model.safetensors.index.json": json.dumps(
            {
                "metadata": {"total_size": 1024},
                "weight_map": {
                    "llm.embed_tokens.weight": shard_1,
                    "audio_vae.decoder.weight": shard_2,
                },
            }
        ).encode("utf-8"),
        "tokenizer.json": json.dumps({"version": "1.0"}).encode("utf-8"),
        "tokenizer_config.json": json.dumps({"tokenizer_class": "BailingTokenizer"}).encode(
            "utf-8"
        ),
        "special_tokens_map.json": json.dumps({"eos_token": "<|endoftext|>"}).encode("utf-8"),
        "chat_template.jinja": b"{{ messages }}",
        "campplus.onnx": b"ONNX_SYNTHETIC_CAMPPLUS",
        shard_1: _build_safetensors_bytes(512),
        shard_2: _build_safetensors_bytes(512),
    }

    manifest = copy.deepcopy(MANIFEST)
    for rel_path, content in files.items():
        target = model_dir / rel_path
        target.write_bytes(content)

    manifest["license_sha256"] = hashlib.sha256(files["LICENSE"]).hexdigest()
    manifest["legal_sha256"] = hashlib.sha256(files["LEGAL.md"]).hexdigest()
    asset_names = (
        "config.json",
        "generation_config.json",
        "model.safetensors.index.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "chat_template.jinja",
        "campplus.onnx",
        shard_1,
        shard_2,
    )
    manifest["assets"] = [
        {
            "path": rel_path,
            "sha256": hashlib.sha256(files[rel_path]).hexdigest(),
            "size": len(files[rel_path]),
        }
        for rel_path in asset_names
    ]
    return model_dir, manifest


class _FakeCudaTensor:
    def __init__(self, device: str = "cuda:0", is_cuda: bool = True) -> None:
        self.device = device
        self.is_cuda = is_cuda


class _FakeCudaModel:
    def __init__(self, device: str = "cuda:0", is_cuda: bool = True) -> None:
        self._tensor = _FakeCudaTensor(device=device, is_cuda=is_cuda)

    def parameters(self):
        return [self._tensor]

    def buffers(self):
        return [self._tensor]


class _FakeWaveformTensor:
    def __init__(self, values: list[float]) -> None:
        self._values = list(values)

    def detach(self):
        return self

    def float(self):
        return self

    def cpu(self):
        return self

    def reshape(self, *_shape):
        return self

    def tolist(self):
        return list(self._values)


@dataclass
class _FakeLingV3TTSGenerationOutput:
    waveform: _FakeWaveformTensor
    stop_probabilities: list[float]
    stopped: bool
    prompt_latent_steps: int = 0


class _FakeMing3Runtime:
    def __init__(
        self,
        *,
        samples=None,
        sample_rate: int = MING3_SAMPLE_RATE,
        truncated: bool = False,
        device: str = "cuda:0",
        is_cuda: bool = True,
        on_generate=None,
    ) -> None:
        self.model = _FakeCudaModel(device=device, is_cuda=is_cuda)
        self.samples = [0.1, -0.15, 0.25, -0.2, 0.05] if samples is None else samples
        self.sample_rate = sample_rate
        self.truncated = truncated
        self.on_generate = on_generate
        self.calls: list[dict] = []
        self.closed = False

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_generate is not None:
            self.on_generate(kwargs)
        return {
            "samples": self.samples,
            "sample_rate": self.sample_rate,
            "truncated": self.truncated,
        }

    def close(self) -> None:
        self.closed = True


def test_ming3_profile_registration_and_pinned_manifest_metadata():
    assert default_voice("Ming 3 Tiny") == MING3_VOICE
    assert validate_voice("Ming 3 Tiny", None) == "US_Emma"
    assert validate_voice("Ming 3 Tiny", "US_Emma") == "US_Emma"
    assert synthesis_mode("Ming 3 Tiny") == "buffered"
    assert artifact_revision("Ming 3 Tiny") == MING3_REVISION
    assert input_limit("Ming 3 Tiny") == MING3_MAX_CHARS
    assert validate_tts_device("Ming 3 Tiny", "cuda") == "cuda"
    with pytest.raises(ValueError, match="Ming 3 Tiny requires GPU"):
        validate_tts_device("Ming 3 Tiny", "cpu")

    # Verify the pinned upstream 30-shard inventory in ming3_manifest.json
    assert MANIFEST["repository"] == "https://huggingface.co/inclusionAI/Ming-3.0-tts-tiny"
    assert MANIFEST["revision"] == "4715ef5a2f02901f75687d538b6920fd0dd11e6b"
    assert MANIFEST["runtime_repository"] == "https://github.com/inclusionAI/Ming-3.0-tts"
    assert MANIFEST["runtime_revision"] == "9332fb9443325d4726ce02cefd4ec1c211df4079"
    assert MANIFEST["license"] == "MIT"
    assert MANIFEST["shard_count"] == 30
    assert MANIFEST["total_shard_bytes"] == 17963396008
    shard_assets = [a for a in MANIFEST["assets"] if a["path"].endswith(".safetensors")]
    assert len(shard_assets) == 30
    assert sum(a["size"] for a in shard_assets) == 17963396008
    asset_paths = {a["path"] for a in MANIFEST["assets"]}
    assert "model.safetensors.index.json" in asset_paths
    assert "campplus.onnx" in asset_paths


def test_verify_ming3_assets_accepts_pinned_tree_and_rejects_tampering(tmp_path: Path):
    model_dir, manifest = _create_synthetic_ming3_dir(tmp_path)
    verified = verify_ming3_assets(model_dir, manifest)
    assert verified == model_dir

    # Tamper with digest
    bad_manifest = copy.deepcopy(manifest)
    bad_manifest["assets"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_ming3_assets(model_dir, bad_manifest)

    # Tamper with size
    size_manifest = copy.deepcopy(manifest)
    size_manifest["assets"][0]["size"] += 4
    with pytest.raises(ValueError, match="size mismatch"):
        verify_ming3_assets(model_dir, size_manifest)

    # Tamper with model.safetensors.index.json to reference an unpinned shard
    bad_index = json.dumps(
        {"weight_map": {"llm.weight": "model-00099-of-00030.safetensors"}}
    ).encode("utf-8")
    (model_dir / "model.safetensors.index.json").write_bytes(bad_index)
    idx_manifest = copy.deepcopy(manifest)
    for entry in idx_manifest["assets"]:
        if entry["path"] == "model.safetensors.index.json":
            entry["sha256"] = hashlib.sha256(bad_index).hexdigest()
            entry["size"] = len(bad_index)
    with pytest.raises(ValueError, match="references unpinned shards"):
        verify_ming3_assets(model_dir, idx_manifest)


def test_verify_ming3_assets_rejects_symlinks_and_remote_code_hooks(tmp_path: Path):
    model_dir, manifest = _create_synthetic_ming3_dir(tmp_path)

    # Reject symlinked model_dir
    link_dir = tmp_path / "symlink-model"
    link_dir.symlink_to(model_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="cannot be a symlink"):
        verify_ming3_assets(link_dir, manifest)

    # Reject symlinked asset file
    outside = tmp_path / "outside.json"
    outside.write_bytes((model_dir / "config.json").read_bytes())
    (model_dir / "config.json").unlink()
    (model_dir / "config.json").symlink_to(outside)
    with pytest.raises(ValueError, match="cannot be a symlink"):
        verify_ming3_assets(model_dir, manifest)

    # Restore regular file with unapproved auto_map remote code hook
    (model_dir / "config.json").unlink()
    malicious_cfg = json.dumps(
        {"model_type": "ming3", "auto_map": {"AutoModel": "evil.py"}}
    ).encode("utf-8")
    (model_dir / "config.json").write_bytes(malicious_cfg)
    manifest["assets"][0]["sha256"] = hashlib.sha256(malicious_cfg).hexdigest()
    manifest["assets"][0]["size"] = len(malicious_cfg)
    with pytest.raises(ValueError, match="rejects remote code execution hooks"):
        verify_ming3_assets(model_dir, manifest)


def test_validate_ming3_request_enforces_ip_task_and_refuses_emotion_conflicts():
    cleaned, voice = validate_ming3_request("  Hello from Ming 3 Tiny.  ")
    assert cleaned == "Hello from Ming 3 Tiny."
    assert voice == "US_Emma"

    with pytest.raises(ValueError, match="does not accept emotion or freeform voice descriptions"):
        validate_ming3_request("Hello", emotion="happy")

    with pytest.raises(ValueError, match="does not accept emotion or freeform voice descriptions"):
        validate_ming3_request("Hello", voice_description="warm alto narrator")

    with pytest.raises(ValueError, match="requires preset speaker task"):
        validate_ming3_request("Hello", task_type="zero_shot")

    with pytest.raises(ValueError, match="requires configured voice US_Emma"):
        validate_ming3_request("Hello", voice="af_heart")

    with pytest.raises(ValueError, match="exceeds 1200 characters"):
        validate_ming3_request("a" * (MING3_MAX_CHARS + 1))


def test_ming3_default_loader_and_official_ming_tiny_tts_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    model_dir, manifest = _create_synthetic_ming3_dir(tmp_path)
    from_pretrained_calls: list[dict] = []
    generate_calls: list[dict] = []
    stopped_flag = {"value": True}

    class _FakeMingTinyTTS:
        sample_rate = MING3_SAMPLE_RATE

        def __init__(self) -> None:
            self.engine = SimpleNamespace(model=_FakeCudaModel(device="cuda:0", is_cuda=True))

        @classmethod
        def from_pretrained(
            cls,
            model_path: str,
            *,
            device: str = "cuda:0",
            dtype: str = "bf16",
            moe_backend: str = "auto",
            runtime_state_dtype: str = "auto",
        ):
            from_pretrained_calls.append(
                {
                    "model_path": model_path,
                    "device": device,
                    "dtype": dtype,
                    "moe_backend": moe_backend,
                    "runtime_state_dtype": runtime_state_dtype,
                }
            )
            return cls()

        def generate(self, *, task: str, text: str, ip: str | None = None, **_kwargs):
            generate_calls.append({"task": task, "text": text, "ip": ip})
            return _FakeLingV3TTSGenerationOutput(
                waveform=_FakeWaveformTensor([0.12, -0.18, 0.22, -0.14]),
                stop_probabilities=[0.05, 0.82],
                stopped=stopped_flag["value"],
                prompt_latent_steps=0,
            )

    fake_module = SimpleNamespace(
        __git_revision__=MING3_RUNTIME_REVISION,
        MingTinyTTS=_FakeMingTinyTTS,
    )
    monkeypatch.setitem(sys.modules, "ming_tiny", fake_module)

    backend = Ming3TinyBackend(model_dir, MING3_VOICE, device="cuda", manifest=manifest)
    assert backend._runtime is None
    assert backend.assets_state == "not_checked"

    wav_bytes = backend.synthesize("Official MingTinyTTS API contract check.")
    assert len(from_pretrained_calls) == 1
    assert from_pretrained_calls[0] == {
        "model_path": str(model_dir),
        "device": "cuda:0",
        "dtype": "bf16",
        "moe_backend": "loop",
        "runtime_state_dtype": "stable_fp32",
    }
    assert generate_calls == [
        {"task": "ip", "text": "Official MingTinyTTS API contract check.", "ip": "US_Emma"}
    ]
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        assert wav_file.getframerate() == MING3_SAMPLE_RATE
        assert wav_file.getnframes() == 4

    # When LingV3TTSGenerationOutput.stopped is False (max_decode_steps reached), fail closed
    stopped_flag["value"] = False
    with pytest.raises(TTSBackendError, match="truncated before natural completion"):
        backend.synthesize("Unstopped generation must fail closed.")


def test_ming3_lazy_load_rejection_cleans_up_and_never_reuses_rejected_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    model_dir, manifest = _create_synthetic_ming3_dir(tmp_path)
    bad_runtime_1 = _FakeMing3Runtime(device="cpu", is_cuda=False)
    bad_runtime_2 = _FakeMing3Runtime(device="cpu", is_cuda=False)
    good_runtime = _FakeMing3Runtime(device="cuda:0", is_cuda=True)
    candidates = [bad_runtime_1, bad_runtime_2, good_runtime]
    loader_calls = 0

    def staged_loader(_dir: Path, _man: dict):
        nonlocal loader_calls
        loader_calls += 1
        return candidates.pop(0)

    monkeypatch.setattr(ming3_mod, "_default_ming3_loader", staged_loader)

    # Construct via lazy path (loader=None)
    backend = Ming3TinyBackend(model_dir, MING3_VOICE, device="cuda", manifest=manifest)
    assert backend._runtime is None
    assert backend.loaded_device is None
    assert backend.assets_state == "not_checked"

    # First lazy synthesize() rejects bad_runtime_1, closes it, and leaves _runtime None
    with pytest.raises(TTSBackendError, match="not resident on CUDA"):
        backend.synthesize("First attempt")
    assert loader_calls == 1
    assert bad_runtime_1.closed is True
    assert bad_runtime_1.calls == []
    assert backend._runtime is None
    assert backend.loaded_device is None
    assert backend.assets_state == "not_checked"

    # Second lazy synthesize() must NOT reuse bad_runtime_1; it calls loader again and rejects bad_runtime_2
    with pytest.raises(TTSBackendError, match="not resident on CUDA"):
        backend.synthesize("Second attempt")
    assert loader_calls == 2
    assert bad_runtime_2.closed is True
    assert bad_runtime_2.calls == []
    assert backend._runtime is None

    # Third lazy synthesize() loads good_runtime on cuda:0 and succeeds
    wav_bytes = backend.synthesize("Third attempt succeeds")
    assert loader_calls == 3
    assert backend._runtime is good_runtime
    assert backend.loaded_device == "cuda:0"
    assert backend.assets_state == "verified"
    assert len(good_runtime.calls) == 1
    assert len(wav_bytes) > 44


def test_ming3_backend_synthesis_residency_truncation_and_cancellation(tmp_path: Path):
    model_dir, manifest = _create_synthetic_ming3_dir(tmp_path)
    runtime = _FakeMing3Runtime()
    backend = Ming3TinyBackend(
        model_dir,
        MING3_VOICE,
        device="cuda",
        manifest=manifest,
        loader=lambda _dir, _man: runtime,
    )
    assert backend.assets_state == "verified"
    assert backend.loaded_device == "cuda:0"

    wav_bytes = backend.synthesize("Synthesize a buffered test phrase.")
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == MING3_SAMPLE_RATE
        assert wav_file.getnframes() == len(runtime.samples)
    assert runtime.calls[-1]["trust_remote_code"] is False
    assert runtime.calls[-1]["local_files_only"] is True
    assert runtime.calls[-1]["task_type"] == "ip"
    assert runtime.calls[-1]["voice"] == "US_Emma"

    # Non-CUDA model tensors fail closed
    with pytest.raises(TTSBackendError, match="not resident on CUDA"):
        Ming3TinyBackend(
            model_dir,
            MING3_VOICE,
            device="cuda",
            manifest=manifest,
            loader=lambda _dir, _man: _FakeMing3Runtime(device="cpu", is_cuda=False),
        )

    # Truncated generation fails closed
    truncated_backend = Ming3TinyBackend(
        model_dir,
        MING3_VOICE,
        device="cuda",
        manifest=manifest,
        loader=lambda _dir, _man: _FakeMing3Runtime(truncated=True),
    )
    with pytest.raises(TTSBackendError, match="truncated before natural completion"):
        truncated_backend.synthesize("Truncated test")

    # Exceeding sample cap fails closed
    overcap_backend = Ming3TinyBackend(
        model_dir,
        MING3_VOICE,
        device="cuda",
        manifest=manifest,
        loader=lambda _dir, _man: _FakeMing3Runtime(samples=[0.1] * (MING3_MAX_SAMPLES + 1)),
    )
    with pytest.raises(TTSBackendError, match="exceeded maximum sample cap"):
        overcap_backend.synthesize("Overcap test")

    # Pre-cancelled synthesis raises SynthesisCancelled
    cancel_event = threading.Event()
    cancel_event.set()
    with pytest.raises(SynthesisCancelled):
        backend.synthesize("Cancelled before start", cancel=cancel_event)

    # Mid-inference cancellation raises SynthesisCancelled
    mid_cancel = threading.Event()
    cancelling_backend = Ming3TinyBackend(
        model_dir,
        MING3_VOICE,
        device="cuda",
        manifest=manifest,
        loader=lambda _dir, _man: _FakeMing3Runtime(on_generate=lambda _kw: mid_cancel.set()),
    )
    with pytest.raises(SynthesisCancelled):
        cancelling_backend.synthesize("Cancelled mid inference", cancel=mid_cancel)


async def test_voice_worker_serves_ming3_tiny_on_cuda(tmp_path: Path):
    model_dir, manifest = _create_synthetic_ming3_dir(tmp_path)
    stt_checkpoint = tmp_path / "tiny.en.pt"
    stt_checkpoint.write_bytes(b"synthetic-stt-checkpoint")
    runtime = _FakeMing3Runtime(samples=[0.2, -0.2, 0.15, -0.15] * 100)

    def factory(profile, configured_dir, selected_voice):
        assert profile == "Ming 3 Tiny"
        assert Path(configured_dir) == model_dir
        assert selected_voice == "US_Emma"
        return Ming3TinyBackend(
            configured_dir,
            selected_voice,
            device="cuda",
            manifest=manifest,
            loader=lambda _dir, _man: runtime,
        )

    app = create_app(
        AUTH,
        tts_profile="Ming 3 Tiny",
        tts_model_dir=str(model_dir),
        tts_factory=factory,
        device="cuda",
        stt_backend="openai-whisper",
        stt_checkpoint=str(stt_checkpoint),
    )
    headers = {"Authorization": "Bearer " + "c" * 32}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://voice.local"
    ) as client:
        initial = (await client.get("/health")).json()
        assert initial["device"] == "cuda"
        assert initial["tts_profile"] == "Ming 3 Tiny"
        assert initial["tts_voice"] == "US_Emma"
        assert initial["synthesis_mode"] == "buffered"
        assert initial["tts_artifact_revision"] == MING3_REVISION
        assert initial["assets_state"] == "not_checked"

        response = await client.post(
            "/v1/audio/speech",
            headers=headers,
            json={"input": "Ming 3 Tiny GPU synthesis test.", "profile": "Ming 3 Tiny"},
        )
        assert response.status_code == 200
        lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert len(lines) >= 1
        assert lines[0]["provider"] == "Ming 3 Tiny"
        assert lines[0]["device"] == "cuda:0"
        assert lines[0]["sample_rate"] == MING3_SAMPLE_RATE
        assert lines[0]["synthesis_mode"] == "buffered"

        after = (await client.get("/health")).json()
        assert after["loaded"] == ["tts"]
        assert after["tts_device"] == "cuda:0"
        assert after["assets_state"] == "verified"
