"""CPU adapter and asset validation with synthetic files/runtimes only."""

import hashlib
import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from packages.providers import tts_backends as tts


def kokoro_runtime(monkeypatch, *, available=True, devices=("cuda:0",), buffers=(), failure=None):
    calls = []

    def parameter(value):
        kind, _, index = value.partition(":")
        return SimpleNamespace(
            device=SimpleNamespace(type=kind, index=int(index) if index else None)
        )

    class Pipeline:
        def __init__(self, **kwargs):
            calls.append(("pipeline", kwargs))
            if failure is not None:
                raise failure
            self.model = SimpleNamespace(
                parameters=lambda: iter(map(parameter, devices)),
                buffers=lambda: iter(map(parameter, buffers)),
            )

        def __call__(self, text, *, voice):
            calls.append(("synthesize", text, voice))
            yield text, "synthetic phonemes", [0.25] * 4

    monkeypatch.setitem(sys.modules, "kokoro", SimpleNamespace(KPipeline=Pipeline))
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            cuda=SimpleNamespace(
                is_available=lambda: available,
                set_device=lambda index: calls.append(("set_device", index)),
            )
        ),
    )
    return calls


def test_kokoro_cuda_is_explicit_and_reports_only_verified_parameter_device(monkeypatch):
    calls = kokoro_runtime(monkeypatch)
    backend = tts.create_tts_backend("Kokoro 82M", device="cuda")
    assert backend.loaded_device is None
    backend.load()
    assert backend.loaded_device == "cuda:0"
    assert calls == [
        ("set_device", 0),
        ("pipeline", {"lang_code": "a", "device": "cuda", "repo_id": "hexgrad/Kokoro-82M"}),
    ]
    assert list(backend.synthesize("Public synthetic speech.")) == [
        tts.AudioWaveform([0.25] * 4, 24000)
    ]


def test_kokoro_cuda_unavailable_never_constructs_or_falls_back(monkeypatch):
    calls = kokoro_runtime(monkeypatch, available=False)
    backend = tts.create_tts_backend("Kokoro 82M", device="cuda")
    with pytest.raises(tts.TTSBackendError):
        backend.load()
    assert backend.loaded_device is None
    assert calls == []


@pytest.mark.parametrize("devices", [("cpu",), ("cuda:1",), ("cuda:0", "cpu"), ()])
def test_kokoro_cuda_wrong_or_empty_placement_refuses_without_cpu_retry(monkeypatch, devices):
    calls = kokoro_runtime(monkeypatch, devices=devices)
    backend = tts.create_tts_backend("Kokoro 82M", device="cuda")
    with pytest.raises(tts.TTSBackendError):
        backend.load()
    assert backend.loaded_device is None
    assert [call[1]["device"] for call in calls if call[0] == "pipeline"] == ["cuda"]


def test_kokoro_cuda_loader_failure_is_not_retried_on_cpu(monkeypatch):
    calls = kokoro_runtime(monkeypatch, failure=RuntimeError("synthetic GPU unavailable"))
    backend = tts.create_tts_backend("Kokoro 82M", device="cuda")
    with pytest.raises(RuntimeError):
        backend.load()
    assert backend.loaded_device is None
    assert [call[1]["device"] for call in calls if call[0] == "pipeline"] == ["cuda"]


def test_kokoro_cuda_refuses_a_cpu_model_buffer(monkeypatch):
    calls = kokoro_runtime(monkeypatch, buffers=("cpu",))
    backend = tts.create_tts_backend("Kokoro 82M", device="cuda")
    with pytest.raises(tts.TTSBackendError):
        backend.load()
    assert backend.loaded_device is None
    assert [call[1]["device"] for call in calls if call[0] == "pipeline"] == ["cuda"]


@pytest.mark.parametrize("boundary", ["before", "waveform", "eof"])
def test_kokoro_cuda_revalidates_placement_at_native_output_boundaries(monkeypatch, boundary):
    calls = kokoro_runtime(monkeypatch)
    backend = tts.create_tts_backend("Kokoro 82M", device="cuda")
    backend.load()

    def move_to_cpu():
        backend.pipeline.model.parameters = lambda: iter(
            [SimpleNamespace(device=SimpleNamespace(type="cpu", index=None))]
        )

    def moved_pipeline(self, text, *, voice):
        if boundary == "waveform":
            move_to_cpu()
        yield text, "synthetic phonemes", [0.25] * 4
        if boundary == "eof":
            move_to_cpu()

    if boundary == "before":
        move_to_cpu()
    else:
        monkeypatch.setattr(type(backend.pipeline), "__call__", moved_pipeline)
    iterator = backend.synthesize("Public synthetic speech.")
    if boundary == "eof":
        assert next(iterator) == tts.AudioWaveform([0.25] * 4, 24000)
    with pytest.raises(tts.TTSBackendError):
        next(iterator)
    assert backend.loaded_device is None
    assert not [call for call in calls if call[0] == "synthesize"]


def test_kokoro_cuda_execution_failure_invalidates_loaded_device(monkeypatch):
    kokoro_runtime(monkeypatch)
    backend = tts.create_tts_backend("Kokoro 82M", device="cuda")
    backend.load()

    def failed_pipeline(self, text, *, voice):
        raise RuntimeError("synthetic GPU computation failed")

    monkeypatch.setattr(type(backend.pipeline), "__call__", failed_pipeline)
    with pytest.raises(RuntimeError):
        list(backend.synthesize("Public synthetic speech."))
    assert backend.loaded_device is None


def test_kokoro_cuda_waveform_boundary_close_retains_verified_backend(monkeypatch):
    kokoro_runtime(monkeypatch)
    backend = tts.create_tts_backend("Kokoro 82M", device="cuda")
    backend.load()
    iterator = backend.synthesize("Public synthetic speech.")
    assert next(iterator) == tts.AudioWaveform([0.25] * 4, 24000)
    iterator.close()
    assert backend.loaded_device == "cuda:0"


def test_kokoro_cuda_close_verification_failure_invalidates_loaded_device(monkeypatch):
    kokoro_runtime(monkeypatch)
    backend = tts.create_tts_backend("Kokoro 82M", device="cuda")
    backend.load()
    iterator = backend.synthesize("Public synthetic speech.")
    next(iterator)

    def failed_parameters():
        raise RuntimeError("synthetic device verification failed")

    backend.pipeline.model.parameters = failed_parameters
    with pytest.raises(RuntimeError):
        iterator.close()
    assert backend.loaded_device is None


def scoped_kokoro_runtime(monkeypatch, *, device="cuda", hip="6.3"):
    kokoro_runtime(monkeypatch, devices=("cuda:0",) if device == "cuda" else ("cpu",))
    state = SimpleNamespace(
        enabled=True,
        benchmark=True,
        benchmark_limit=17,
        deterministic=True,
        allow_tf32=False,
        fp32_precision="ieee",
    )
    contexts = []

    @contextmanager
    def flags(**values):
        original = {key: value for key, value in vars(state).items() if key != "flags"}
        assert values == {
            key: False if key == "enabled" else value for key, value in original.items()
        }
        contexts.append(values)
        vars(state).update(values)
        try:
            yield
        finally:
            vars(state).update(original)

    sys.modules["torch"].version = SimpleNamespace(hip=hip)
    sys.modules["torch"].backends = SimpleNamespace(
        cudnn=SimpleNamespace(**vars(state), flags=flags)
    )
    # The context mutates the same object whose flags the production code reads.
    backend_flags = sys.modules["torch"].backends.cudnn
    state = backend_flags
    original = {key: value for key, value in vars(state).items() if key != "flags"}
    backend = tts.create_tts_backend("Kokoro 82M", device=device)
    backend.load()
    return backend, state, original, contexts


@pytest.mark.parametrize("device,hip", [("cuda", "6.3"), ("cuda", None), ("cpu", "6.3")])
@pytest.mark.parametrize("cancel", [False, True])
def test_rocm_kokoro_restores_flags_before_yield_eof_and_cancel(monkeypatch, device, hip, cancel):
    backend, state, original, contexts = scoped_kokoro_runtime(monkeypatch, device=device, hip=hip)
    events = []
    scoped = device == "cuda" and hip is not None

    class Native:
        index = 0

        def __iter__(self):
            events.append(("iter", state.enabled))
            return self

        def __next__(self):
            events.append(("next", state.enabled))
            if self.index:
                raise StopIteration
            self.index += 1
            return "text", "phonemes", [0.25] * 4

        def close(self):
            events.append(("close", state.enabled))

    def native(self, text, *, voice):
        events.append(("create", state.enabled))
        return Native()

    monkeypatch.setattr(type(backend.pipeline), "__call__", native)
    iterator = backend.synthesize("Public synthetic speech.")
    assert next(iterator) == tts.AudioWaveform([0.25] * 4, 24000)
    assert {key: getattr(state, key) for key in original} == original
    if cancel:
        iterator.close()
    else:
        with pytest.raises(StopIteration):
            next(iterator)
    assert {key: getattr(state, key) for key in original} == original
    assert all(enabled is (not scoped) for _, enabled in events)
    assert [name for name, _ in events].count("close") == 1
    assert bool(contexts) is scoped
    assert backend.loaded_device == ("cuda:0" if device == "cuda" else "cpu")


@pytest.mark.parametrize("failure", ["next", "close"])
def test_rocm_kokoro_native_error_restores_flags_and_invalidates_device(monkeypatch, failure):
    backend, state, original, _ = scoped_kokoro_runtime(monkeypatch)

    class Native:
        def __iter__(self):
            return self

        def __next__(self):
            assert state.enabled is False
            if failure == "next":
                raise RuntimeError("synthetic native failure")
            raise StopIteration

        def close(self):
            assert state.enabled is False
            if failure == "close":
                raise RuntimeError("synthetic native cleanup failure")

    monkeypatch.setattr(type(backend.pipeline), "__call__", lambda *args, **kwargs: Native())
    with pytest.raises(RuntimeError):
        list(backend.synthesize("Public synthetic speech."))
    assert {key: getattr(state, key) for key in original} == original
    assert backend.loaded_device is None


@pytest.mark.parametrize("cancel", [False, True])
def test_rocm_kokoro_close_moved_model_invalidates_device(monkeypatch, cancel):
    backend, state, original, _ = scoped_kokoro_runtime(monkeypatch)

    class Native:
        index = 0

        def __iter__(self):
            return self

        def __next__(self):
            assert state.enabled is False
            if self.index:
                raise StopIteration
            self.index += 1
            return "text", "phonemes", [0.25] * 4

        def close(self):
            assert state.enabled is False
            backend.pipeline.model.parameters = lambda: iter(
                [SimpleNamespace(device=SimpleNamespace(type="cpu", index=None))]
            )

    monkeypatch.setattr(type(backend.pipeline), "__call__", lambda *args, **kwargs: Native())
    iterator = backend.synthesize("Public synthetic speech.")
    assert next(iterator) == tts.AudioWaveform([0.25] * 4, 24000)
    with pytest.raises(tts.TTSBackendError):
        iterator.close() if cancel else next(iterator)
    assert {key: getattr(state, key) for key in original} == original
    assert backend.loaded_device is None


def test_kokoro_default_cpu_stays_explicit(monkeypatch):
    calls = kokoro_runtime(monkeypatch, available=False, devices=("cpu",))
    backend = tts.create_tts_backend("Kokoro 82M")
    backend.load()
    assert backend.loaded_device == "cpu"
    assert calls == [
        ("pipeline", {"lang_code": "a", "device": "cpu", "repo_id": "hexgrad/Kokoro-82M"})
    ]


@pytest.mark.parametrize(
    "profile,device",
    [("Supertonic 3", "cuda"), ("VibeVoice Realtime 0.5B", "cuda"), ("Kokoro 82M", "auto")],
)
def test_unsupported_tts_device_selection_refuses_before_optional_import(profile, device):
    with pytest.raises(ValueError):
        tts.create_tts_backend(profile, device=device)


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
