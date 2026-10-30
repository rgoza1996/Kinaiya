"""Pinned GPU STT boundaries, using synthetic bytes and fake runtimes only."""

import hashlib
from array import array
from types import SimpleNamespace

import pytest

from packages.providers import openai_whisper_backend as stt


def fake_model_runtime(
    monkeypatch, tmp_path, *, available=True, devices=("cuda:0",), buffer_devices=("cuda:0",)
):
    checkpoint = b"synthetic pinned checkpoint; never a real model"
    path = tmp_path / "tiny.en.pt"
    path.write_bytes(checkpoint)
    monkeypatch.setitem(stt._OFFICIAL_SHA256, "tiny.en", hashlib.sha256(checkpoint).hexdigest())
    calls = []

    class Model:
        def __init__(self, dimensions):
            self.devices = devices
            self.buffer_devices = buffer_devices
            self.result = {"text": " Public synthetic speech. ", "language": "en"}
            calls.append(("construct", dimensions))

        def parameters(self):
            return iter(SimpleNamespace(device=value) for value in self.devices)

        def buffers(self):
            return iter(SimpleNamespace(device=value) for value in self.buffer_devices)

        def load_state_dict(self, state):
            calls.append(("state", state))

        def to(self, device):
            calls.append(("to", device))
            return self

        def eval(self):
            calls.append(("eval",))

        def transcribe(self, audio, **kwargs):
            calls.append(("transcribe", audio, kwargs))
            return self.result

    def load(source, **kwargs):
        calls.append(("load", source.read(), kwargs))
        # Changing the path after identity validation cannot change loaded bytes.
        path.write_bytes(b"replaced after validation")
        return {"dims": {"n_audio_ctx": 1500}, "model_state_dict": {"synthetic": 1}}

    runtimes = {
        "torch": SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: available), load=load),
        "whisper": SimpleNamespace(ModelDimensions=lambda **kwargs: kwargs, Whisper=Model),
    }

    def import_module(name):
        calls.append(("import", name))
        return runtimes[name]

    monkeypatch.setattr(stt.importlib, "import_module", import_module)
    monkeypatch.setattr(stt, "_decode_audio", lambda data: ("decoded", data))
    return path, calls


def test_production_checkpoint_allowlist_matches_official_openai_identities():
    assert stt._OFFICIAL_SHA256 == {
        "tiny.en": "d3dd57d32accea0b295c96e26691aa14d8822fac7d9d27d5dc00b4ca2826dd03",
        "tiny": "65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9",
    }


def test_constructor_and_health_placement_are_lazy(monkeypatch, tmp_path):
    def unexpected(name):
        pytest.fail(f"Constructor imported optional runtime {name}")

    monkeypatch.setattr(stt.importlib, "import_module", unexpected)
    backend = stt.OpenAIWhisperBackend(tmp_path / "not-yet-present.pt")
    assert backend.loaded_device is None
    assert backend.device is None
    assert backend.requested_device == "cuda:0"


@pytest.mark.parametrize("device", ["cpu", "mps", "hip", "cuda:-1", "cuda:0/cpu", None])
def test_non_gpu_or_malformed_device_is_rejected(tmp_path, device):
    with pytest.raises(ValueError):
        stt.OpenAIWhisperBackend(tmp_path / "checkpoint.pt", device=device)


def test_arbitrary_checkpoint_model_name_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        stt.OpenAIWhisperBackend(tmp_path / "checkpoint.pt", model="untrusted-local-model")


@pytest.mark.parametrize("checkpoint", ["tiny.en.pt", "./tiny.en.pt", "~/tiny.en.pt"])
def test_relative_checkpoint_path_is_rejected_instead_of_resolved(checkpoint):
    with pytest.raises(ValueError, match="absolute"):
        stt.OpenAIWhisperBackend(checkpoint)


def test_gpu_result_uses_verified_device_and_loads_same_pinned_bytes_once(monkeypatch, tmp_path):
    path, calls = fake_model_runtime(monkeypatch, tmp_path)
    original = path.read_bytes()
    backend = stt.OpenAIWhisperBackend(path)
    expected = {
        "text": "Public synthetic speech.",
        "language": "en",
        "final": True,
        "provider": "openai-whisper",
        "model": "tiny.en",
        "device": "cuda:0",
    }
    assert backend.transcribe(b"first public audio") == expected
    assert backend.transcribe(b"second public audio") == expected
    assert backend.loaded_device == backend.device == "cuda:0"
    assert [call for call in calls if call[0] == "load"] == [
        ("load", original, {"map_location": "cpu", "weights_only": True})
    ]
    assert [call for call in calls if call[0] == "to"] == [("to", "cuda:0")]
    inferences = [call for call in calls if call[0] == "transcribe"]
    assert len(inferences) == 2
    assert inferences[0][1] == ("decoded", b"first public audio")
    assert inferences[0][2] == {
        "language": "en",
        "task": "transcribe",
        "fp16": True,
        "temperature": 0,
        "beam_size": 1,
        "condition_on_previous_text": False,
        "word_timestamps": False,
        "without_timestamps": True,
        "verbose": None,
    }


def test_unavailable_gpu_does_not_load_or_construct_a_cpu_model(monkeypatch, tmp_path):
    path, calls = fake_model_runtime(monkeypatch, tmp_path, available=False)
    backend = stt.OpenAIWhisperBackend(path)
    with pytest.raises(stt.STTBackendError, match="GPU runtime"):
        backend.transcribe(b"public audio")
    assert backend.loaded_device is None
    assert calls == [("import", "torch")]


@pytest.mark.parametrize("devices", [("cpu",), ("cuda:1",), ("cuda:0", "cpu"), ()])
def test_wrong_mixed_or_empty_model_placement_refuses_result(monkeypatch, tmp_path, devices):
    path, calls = fake_model_runtime(monkeypatch, tmp_path, devices=devices)
    backend = stt.OpenAIWhisperBackend(path)
    with pytest.raises(stt.STTBackendError, match="wholly"):
        backend.transcribe(b"public audio")
    assert backend.loaded_device is None
    assert backend._model is None
    assert not any(call[0] == "transcribe" for call in calls)
    assert [call for call in calls if call[0] == "to"] == [("to", "cuda:0")]


@pytest.mark.parametrize("buffer_devices", [("cpu",), ("cuda:1",), ("cuda:0", "cpu")])
def test_gpu_parameters_with_wrong_registered_buffer_refuse_inference(
    monkeypatch, tmp_path, buffer_devices
):
    path, calls = fake_model_runtime(monkeypatch, tmp_path, buffer_devices=buffer_devices)
    backend = stt.OpenAIWhisperBackend(path)
    with pytest.raises(stt.STTBackendError, match="wholly"):
        backend.transcribe(b"public audio")
    assert backend.loaded_device is None
    assert backend._model is None
    assert not any(call[0] == "transcribe" for call in calls)


def test_registered_buffer_changed_during_inference_is_not_published(monkeypatch, tmp_path):
    path, _ = fake_model_runtime(monkeypatch, tmp_path)
    backend = stt.OpenAIWhisperBackend(path)
    backend.transcribe(b"public audio")

    def moved_buffer_to_cpu(audio, **kwargs):
        backend._model.buffer_devices = ("cpu",)
        return {"text": "Do not publish.", "language": "en"}

    backend._model.transcribe = moved_buffer_to_cpu
    with pytest.raises(stt.STTBackendError, match="wholly"):
        backend.transcribe(b"public audio")
    assert backend.loaded_device is None


def test_device_changed_during_inference_is_not_published(monkeypatch, tmp_path):
    path, _ = fake_model_runtime(monkeypatch, tmp_path)
    backend = stt.OpenAIWhisperBackend(path)
    backend.transcribe(b"public audio")

    def moved_to_cpu(audio, **kwargs):
        backend._model.devices = ("cpu",)
        return {"text": "Do not publish.", "language": "en"}

    backend._model.transcribe = moved_to_cpu
    with pytest.raises(stt.STTBackendError, match="wholly"):
        backend.transcribe(b"public audio")
    assert backend.loaded_device is None


def test_unpinned_bytes_are_rejected_before_any_model_runtime_import(monkeypatch, tmp_path):
    path = tmp_path / "tiny.en.pt"
    path.write_bytes(b"untrusted model pickle")

    def unexpected(name):
        pytest.fail(f"Unpinned checkpoint imported {name}")

    monkeypatch.setattr(stt.importlib, "import_module", unexpected)
    monkeypatch.setattr(stt, "_decode_audio", lambda data: data)
    backend = stt.OpenAIWhisperBackend(path)
    with pytest.raises(stt.STTBackendError, match="identity"):
        backend.transcribe(b"public audio")
    assert backend.loaded_device is None


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "fifo", "oversized"])
def test_checkpoint_must_be_bounded_regular_local_file(monkeypatch, tmp_path, kind):
    path = tmp_path / "checkpoint.pt"
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target.pt"
        target.write_bytes(b"not trusted")
        path.symlink_to(target)
    elif kind == "fifo":
        stt.os.mkfifo(path)
    elif kind == "oversized":
        monkeypatch.setattr(stt, "MAX_CHECKPOINT_BYTES", 3)
        path.write_bytes(b"four")
    backend = stt.OpenAIWhisperBackend(path)
    with pytest.raises(stt.STTBackendError):
        backend._checkpoint_bytes()


@pytest.mark.parametrize(
    "result",
    [
        None,
        {"text": 1, "language": "en"},
        {"text": "x", "language": "en/private"},
        {"text": "x" * (stt.MAX_TEXT_CHARS + 1), "language": "en"},
    ],
)
def test_invalid_or_unbounded_model_output_is_rejected(monkeypatch, tmp_path, result):
    path, _ = fake_model_runtime(monkeypatch, tmp_path)
    backend = stt.OpenAIWhisperBackend(path)
    backend.transcribe(b"public audio")
    backend._model.result = result
    with pytest.raises(stt.STTBackendError, match="transcript"):
        backend.transcribe(b"public audio")


class Frame:
    def __init__(self, samples, *, rate=16000, raw=None):
        self.samples = samples
        self.sample_rate = rate
        self.pts = 42
        self.raw = raw

    def to_ndarray(self):
        raw = self.raw if self.raw is not None else b"\0\0" * self.samples
        return SimpleNamespace(tobytes=lambda: raw)


def fake_audio_runtime(monkeypatch, frames, *, outputs=None, flush=(), decode_failure=None):
    calls = []

    class Container:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append(("close",))

        def decode(self, **kwargs):
            assert kwargs == {"audio": 0}
            if decode_failure:
                raise decode_failure
            yield from frames

    class Resampler:
        def __init__(self, **kwargs):
            calls.append(("resampler", kwargs))

        def resample(self, frame):
            calls.append(("resample", frame))
            if frame is None:
                return flush
            return outputs if outputs is not None else [frame]

    class FloatAudio:
        def __init__(self, raw):
            self.raw = bytes(raw)

        def astype(self, dtype):
            assert dtype == "float32"
            return self

        def __truediv__(self, divisor):
            self.divisor = divisor
            return self

        def values(self):
            result = array("h")
            result.frombytes(self.raw)
            return [value / self.divisor for value in result]

    def frombuffer(raw, *, dtype):
        assert dtype == "int16"
        return FloatAudio(raw)

    def open_audio(source, **kwargs):
        assert source.read() == b"synthetic encoded audio"
        assert kwargs == {
            "mode": "r",
            "metadata_errors": "ignore",
            "options": {"protocol_whitelist": ""},
        }
        return Container()

    modules = {
        "av": SimpleNamespace(AudioResampler=Resampler, open=open_audio),
        "numpy": SimpleNamespace(frombuffer=frombuffer, int16="int16", float32="float32"),
    }
    monkeypatch.setattr(stt.importlib, "import_module", modules.__getitem__)
    return calls


def test_decodes_in_memory_to_mono_16khz_float_audio_and_flushes(monkeypatch):
    source = Frame(3, rate=48000)
    output = Frame(2, raw=array("h", [-32768, 16384]).tobytes())
    final = Frame(1, raw=array("h", [32767]).tobytes())
    calls = fake_audio_runtime(monkeypatch, [source], outputs=[output], flush=[final])
    audio = stt._decode_audio(b"synthetic encoded audio")
    assert audio.values() == [-1.0, 0.5, 32767 / 32768.0]
    assert source.pts is None
    assert calls == [
        ("resampler", {"format": "s16", "layout": "mono", "rate": 16000}),
        ("resample", source),
        ("resample", None),
        ("close",),
    ]


@pytest.mark.parametrize("data", [b"", "not bytes", b"x" * (stt.MAX_AUDIO_BYTES + 1)])
def test_empty_wrong_type_or_oversized_upload_rejected_before_decoder_import(monkeypatch, data):
    def unexpected(name):
        pytest.fail(f"Invalid upload imported optional runtime {name}")

    monkeypatch.setattr(stt.importlib, "import_module", unexpected)
    with pytest.raises(stt.STTInputError):
        stt._decode_audio(data)


def test_exact_60_second_pcm_is_accepted(monkeypatch):
    fake_audio_runtime(monkeypatch, [Frame(stt.MAX_SAMPLES)])
    audio = stt._decode_audio(b"synthetic encoded audio")
    assert len(audio.raw) == stt.MAX_SAMPLES * 2


@pytest.mark.parametrize(
    "frames,outputs,flush",
    [
        ([Frame(48000 * 60 + 1, rate=48000)], [], []),
        ([Frame(16000 * 30), Frame(16000 * 30 + 1)], [], []),
        ([Frame(1)], [Frame(stt.MAX_SAMPLES + 1)], []),
        ([Frame(stt.MAX_SAMPLES)], None, [Frame(1)]),
    ],
)
def test_source_resampled_and_flush_duration_bounds_reject_without_truncation(
    monkeypatch, frames, outputs, flush
):
    fake_audio_runtime(monkeypatch, frames, outputs=outputs, flush=flush)
    with pytest.raises(stt.STTInputError, match="60 seconds"):
        stt._decode_audio(b"synthetic encoded audio")


@pytest.mark.parametrize("frame", [Frame(1, rate=0), Frame(-1), Frame(True), Frame(1, raw=b"x")])
def test_invalid_frame_timing_or_pcm_shape_is_rejected(monkeypatch, frame):
    fake_audio_runtime(monkeypatch, [frame])
    with pytest.raises(stt.STTInputError):
        stt._decode_audio(b"synthetic encoded audio")


@pytest.mark.parametrize("failure", [None, ValueError("synthetic malformed codec")])
def test_empty_or_malformed_audio_is_rejected_and_closed(monkeypatch, failure):
    calls = fake_audio_runtime(monkeypatch, [], decode_failure=failure)
    with pytest.raises(stt.STTInputError):
        stt._decode_audio(b"synthetic encoded audio")
    assert calls[-1] == ("close",)
