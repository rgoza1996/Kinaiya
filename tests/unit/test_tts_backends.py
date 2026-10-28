"""CPU adapter and asset validation with synthetic files/runtimes only."""

import hashlib
import json
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from packages.providers import tts_backends as tts


def asset_tree(tmp_path, monkeypatch):
    root = tmp_path / "models"
    files = {}
    for relative in (
        "LICENSE",
        "onnx/tts.json",
        "onnx/unicode_indexer.json",
        "onnx/duration_predictor.onnx",
        "onnx/text_encoder.onnx",
        "onnx/vector_estimator.onnx",
        "onnx/vocoder.onnx",
        "voice_styles/F1.json",
    ):
        data = f"Public synthetic artifact: {relative}".encode()
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files[relative] = {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"revision": tts.SUPERTONIC_REVISION, "files": files}))
    monkeypatch.setattr(tts, "_MANIFEST", manifest)
    return root, manifest


def fake_sdk(monkeypatch, *, providers=("CPUExecutionProvider",), rate=44100):
    calls = []
    loader = SimpleNamespace(DEFAULT_ONNX_PROVIDERS=["CUDAExecutionProvider"])

    class Pipeline:
        def __init__(self, **kwargs):
            calls.append(("construct", kwargs))
            assert loader.DEFAULT_ONNX_PROVIDERS == ["CPUExecutionProvider"]
            self.sample_rate = rate
            session = SimpleNamespace(get_providers=lambda: list(providers))
            self.model = SimpleNamespace(
                **{
                    name: session
                    for name in ("dp_ort", "text_enc_ort", "vector_est_ort", "vocoder_ort")
                }
            )

        def get_voice_style(self, voice):
            calls.append(("style", voice))
            return "synthetic-style"

        def synthesize(self, **kwargs):
            calls.append(("synthesize", kwargs))
            # Preserve all real samples, even if a predicted duration is short.
            return [[0.0, 0.25, -0.25, 0.5]], [0.000001]

    monkeypatch.setitem(sys.modules, "supertonic", SimpleNamespace(TTS=Pipeline, loader=loader))
    monkeypatch.setattr(
        tts.importlib.metadata,
        "version",
        lambda name: "1.3.1" if name == "supertonic" else "1.23.1",
    )
    return calls


def test_provider_module_and_factory_have_no_optional_runtime_imports():
    script = """
import sys
from packages.providers.tts_backends import create_tts_backend
create_tts_backend('Kokoro 82M')
create_tts_backend('Supertonic 3', '/does/not/exist')
assert not {'kokoro', 'numpy', 'supertonic', 'onnxruntime', 'faster_whisper', 'vibevoice', 'transformers', 'diffusers', 'torch'} & sys.modules.keys()
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("voice", list(tts.SUPERTONIC_VOICES))
def test_selected_official_voice_allowlist(voice):
    assert tts.validate_voice("Supertonic 3", voice) == voice


@pytest.mark.parametrize(
    "profile,voice",
    [
        ("Ming 3 Tiny", None),
        ("unknown", "F1"),
        ("Supertonic 3", "../F1"),
        ("Supertonic 3", "af_heart"),
        ("Kokoro 82M", "F1"),
        ("Supertonic 3", []),
        ("VibeVoice Realtime 0.5B", "../en-Carter_man"),
        ("VibeVoice Realtime 0.5B", "af_heart"),
        ([], None),
    ],
)
def test_configuration_cannot_select_arbitrary_runtime_or_voice(profile, voice):
    with pytest.raises(ValueError):
        tts.validate_voice(profile, voice)


def test_checked_in_manifest_has_all_public_pinned_assets():
    manifest = json.loads(tts._MANIFEST.read_text())
    assert manifest["repository"] == "supertone-oss-archive/supertonic-3"
    assert manifest["revision"] == tts.SUPERTONIC_REVISION
    assert len(manifest["files"]) == 17
    assert (
        sum(
            record["size_bytes"]
            for name, record in manifest["files"].items()
            if name.endswith(".onnx")
        )
        == 398075273
    )
    for record in manifest["files"].values():
        assert len(record["sha256"]) == 64 and int(record["sha256"], 16) >= 0
        assert record["size_bytes"] > 0


def test_assets_require_exact_hash_and_size_before_sdk_import(tmp_path, monkeypatch):
    root, _ = asset_tree(tmp_path, monkeypatch)
    tts._verify_assets(root, "F1")
    target = root / "onnx/tts.json"
    original = target.read_bytes()
    target.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(tts.TTSBackendError):
        tts._verify_assets(root, "F1")
    target.write_bytes(original + b"extra")
    with pytest.raises(tts.TTSBackendError):
        tts._verify_assets(root, "F1")


@pytest.mark.parametrize("kind", ["root", "directory", "file"])
def test_asset_verification_refuses_symlinks(tmp_path, monkeypatch, kind):
    root, _ = asset_tree(tmp_path, monkeypatch)
    if kind == "root":
        link = tmp_path / "linked-models"
        link.symlink_to(root, target_is_directory=True)
        root = link
    elif kind == "directory":
        actual = tmp_path / "actual-onnx"
        (root / "onnx").rename(actual)
        (root / "onnx").symlink_to(actual, target_is_directory=True)
    else:
        target = root / "onnx/tts.json"
        actual = tmp_path / "actual.json"
        target.rename(actual)
        target.symlink_to(actual)
    with pytest.raises((OSError, tts.TTSBackendError)):
        tts._verify_assets(root, "F1")


@pytest.mark.parametrize("kind", ["missing", "wrong_revision", "missing_manifest_record"])
def test_incomplete_assets_fail_closed(tmp_path, monkeypatch, kind):
    root, manifest = asset_tree(tmp_path, monkeypatch)
    if kind == "missing":
        (root / "voice_styles/F1.json").unlink()
    else:
        payload = json.loads(manifest.read_text())
        if kind == "wrong_revision":
            payload["revision"] = "0" * 40
        else:
            payload["files"].pop("onnx/tts.json")
        manifest.write_text(json.dumps(payload))
    with pytest.raises((OSError, tts.TTSBackendError)):
        tts._verify_assets(root, "F1")


def test_official_sdk_is_pinned_offline_cpu_and_keeps_entire_waveform(tmp_path, monkeypatch):
    root, _ = asset_tree(tmp_path, monkeypatch)
    calls = fake_sdk(monkeypatch)
    backend = tts.create_tts_backend("Supertonic 3", root)
    assert backend.assets_state == "not_checked" and calls == []
    backend.load()
    assert backend.assets_state == "verified"
    assert calls[0] == (
        "construct",
        {
            "model": "supertonic-3",
            "model_dir": str(root),
            "auto_download": False,
            "intra_op_num_threads": 4,
            "inter_op_num_threads": 1,
        },
    )
    assert calls[1] == ("style", "F1")
    waveform = list(backend.synthesize("Public synthetic text"))[0]
    assert waveform.sample_rate == 44100
    assert waveform.samples == [[0.0, 0.25, -0.25, 0.5]]
    assert calls[2] == (
        "synthesize",
        {
            "text": "Public synthetic text",
            "voice_style": "synthetic-style",
            "total_steps": 8,
            "speed": 1.05,
            "lang": "en",
        },
    )


def test_wrong_runtime_version_rejected_before_construction(tmp_path, monkeypatch):
    root, _ = asset_tree(tmp_path, monkeypatch)
    calls = fake_sdk(monkeypatch)
    monkeypatch.setattr(tts.importlib.metadata, "version", lambda name: "9.0")
    with pytest.raises(tts.TTSBackendError):
        tts.create_tts_backend("Supertonic 3", root).load()
    assert calls == []


def test_wrong_onnxruntime_version_rejected_before_construction(tmp_path, monkeypatch):
    root, _ = asset_tree(tmp_path, monkeypatch)
    calls = fake_sdk(monkeypatch)
    monkeypatch.setattr(
        tts.importlib.metadata, "version", lambda name: "1.3.1" if name == "supertonic" else "1.99"
    )
    with pytest.raises(tts.TTSBackendError):
        tts.create_tts_backend("Supertonic 3", root).load()
    assert calls == []


@pytest.mark.parametrize(
    "providers,rate",
    [
        (("CUDAExecutionProvider", "CPUExecutionProvider"), 44100),
        (("CPUExecutionProvider",), 24000),
    ],
)
def test_wrong_execution_provider_or_sample_rate_fails_closed(
    tmp_path, monkeypatch, providers, rate
):
    root, _ = asset_tree(tmp_path, monkeypatch)
    calls = fake_sdk(monkeypatch, providers=providers, rate=rate)
    backend = tts.create_tts_backend("Supertonic 3", root)
    with pytest.raises(tts.TTSBackendError):
        backend.load()
    assert not any(call[0] == "style" for call in calls)
    assert backend.assets_state == "not_checked"


def test_corrupt_assets_never_construct_sdk(tmp_path, monkeypatch):
    root, _ = asset_tree(tmp_path, monkeypatch)
    calls = fake_sdk(monkeypatch)
    (root / "onnx/vocoder.onnx").write_bytes(b"bad")
    with pytest.raises(tts.TTSBackendError):
        tts.create_tts_backend("Supertonic 3", root).load()
    assert calls == []


def test_kokoro_preserves_sentence_yields_and_cpu_offline_loading(monkeypatch):
    calls = []

    class Pipeline:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def __call__(self, text, voice):
            calls.append((text, voice))
            yield "one", "one", [0.0, 0.5]
            yield "two", "two", [0.25, -0.5]

    monkeypatch.setitem(sys.modules, "kokoro", SimpleNamespace(KPipeline=Pipeline))
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")
    backend = tts.create_tts_backend("Kokoro 82M")
    backend.load()
    assert calls == [{"lang_code": "a", "device": "cpu", "repo_id": "hexgrad/Kokoro-82M"}]
    frames = list(backend.synthesize("One. Two."))
    assert [frame.sample_rate for frame in frames] == [24000, 24000]
    assert [frame.samples for frame in frames] == [[0.0, 0.5], [0.25, -0.5]]


def test_native_producer_receives_same_cancellation_and_backpressure_callback():
    cancelled = threading.Event()
    calls = []

    def emit(waveform):
        calls.append(waveform)
        cancelled.set()

    def produce(text, callback, token):
        assert text == "Public synthetic text" and callback is emit and token is cancelled
        callback(tts.AudioWaveform([0.5], 24000))
        assert token.is_set()

    tts.produce_synthesis(
        SimpleNamespace(produce=produce), "Public synthetic text", emit, cancelled
    )
    assert calls == [tts.AudioWaveform([0.5], 24000)]


def test_legacy_generator_stops_between_waveforms_and_closes():
    cancelled = threading.Event()
    calls = []

    def synthesize(text):
        try:
            yield tts.AudioWaveform([0.5], 24000)
            raise AssertionError("Obsolete generation resumed")
        finally:
            calls.append("closed")

    def emit(waveform):
        calls.append(waveform)
        cancelled.set()

    tts.produce_synthesis(SimpleNamespace(synthesize=synthesize), "Public", emit, cancelled)
    assert calls == [tts.AudioWaveform([0.5], 24000), "closed"]
    tts.produce_synthesis(SimpleNamespace(), "Public", emit, cancelled)
