"""Deterministic unit and worker integration tests for Ming 3 Tiny TTS (#72)."""

import copy
import hashlib
import io
import json
import struct
import threading
import wave
from pathlib import Path

import httpx
import pytest

from packages.providers.ming3_backend import (
    MANIFEST,
    MING3_MAX_CHARS,
    MING3_MAX_SAMPLES,
    MING3_REVISION,
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
    (model_dir / "preset_speakers").mkdir(parents=True)

    files = {
        "LICENSE": b"Apache License\nVersion 2.0, January 2004\n",
        "config.json": json.dumps(
            {
                "model_type": "ming3_tts_tiny",
                "sample_rate": MING3_SAMPLE_RATE,
                "trust_remote_code": False,
            }
        ).encode("utf-8"),
        "tokenizer.json": json.dumps({"version": "1.0"}).encode("utf-8"),
        "tokenizer_config.json": json.dumps({"tokenizer_class": "PreTrainedTokenizerFast"}).encode(
            "utf-8"
        ),
        "model.safetensors": _build_safetensors_bytes(512),
        "preset_speakers/US_Emma.pt": b"FAKE_PT_SPEAKER_EMBEDDING_US_EMMA",
    }

    manifest = copy.deepcopy(MANIFEST)
    for rel_path, content in files.items():
        target = model_dir / rel_path
        target.write_bytes(content)

    manifest["license_sha256"] = hashlib.sha256(files["LICENSE"]).hexdigest()
    manifest["assets"] = [
        {
            "path": rel_path,
            "sha256": hashlib.sha256(files[rel_path]).hexdigest(),
            "size": len(files[rel_path]),
        }
        for rel_path in (
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "model.safetensors",
            "preset_speakers/US_Emma.pt",
        )
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


def test_ming3_profile_registration_and_metadata():
    assert default_voice("Ming 3 Tiny") == MING3_VOICE
    assert validate_voice("Ming 3 Tiny", None) == "US_Emma"
    assert validate_voice("Ming 3 Tiny", "US_Emma") == "US_Emma"
    assert synthesis_mode("Ming 3 Tiny") == "buffered"
    assert artifact_revision("Ming 3 Tiny") == MING3_REVISION
    assert input_limit("Ming 3 Tiny") == MING3_MAX_CHARS
    assert validate_tts_device("Ming 3 Tiny", "cuda") == "cuda"
    with pytest.raises(ValueError, match="Ming 3 Tiny requires GPU"):
        validate_tts_device("Ming 3 Tiny", "cpu")


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

    # Restore regular file with auto_map remote code hook
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
