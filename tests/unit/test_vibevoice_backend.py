"""Pinned runtime/asset and native callback checks; no optional inference imports."""

import hashlib
import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.providers import vibevoice_backend as vv


def assets(tmp_path, monkeypatch):
    root = tmp_path / "assets"
    files = {}
    for relative in vv._REQUIRED:
        value = f"public synthetic asset {relative}".encode()
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        files[relative] = {"size_bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    manifest = {
        "revision": vv.VIBEVOICE_REVISION,
        "runtime_revision": vv.RUNTIME_REVISION,
        "tokenizer_revision": vv.TOKENIZER_REVISION,
        "voice": vv.VIBEVOICE_VOICE,
        "runtime_versions": {
            "kinaiya-vibevoice-runtime": "1.0.0+kinaiya.1",
            "transformers": "4.51.3",
            "torch": "2.14.1+cpu",
        },
        "files": files,
        "runtime_files": {"vibevoice/__init__.py": files["checkpoint/README.md"]},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(vv, "_MANIFEST", path)
    return root, path


class Vector(list):
    @property
    def ndim(self):
        return len(self.shape)

    @property
    def shape(self):
        return (len(self),)


class Tensor:
    def __init__(self, values):
        self.values = values

    def detach(self):
        return self

    def cpu(self):
        return self

    def to(self, dtype):
        assert dtype == "float32"
        return self

    def numpy(self):
        return self.values


class Tokens:
    def __init__(self, width):
        self.shape = (1, width)

    def to(self, device):
        assert device == "cpu"
        return self


def fake_runtime(monkeypatch, *, device="cpu"):
    calls = []
    scheduler = SimpleNamespace(config="pinned-scheduler")
    scheduler.from_config = lambda config, **kwargs: (
        calls.append(("scheduler", config, kwargs)) or scheduler
    )

    class Model:
        model = SimpleNamespace(noise_scheduler=scheduler)
        action = None

        def eval(self):
            calls.append(("eval",))

        def parameters(self):
            return [SimpleNamespace(device=SimpleNamespace(type=device), dtype="float32")]

        def set_ddpm_inference_steps(self, **kwargs):
            calls.append(("steps", kwargs))

        def generate(self, **kwargs):
            calls.append(("generate", kwargs, threading.get_ident()))
            kwargs["all_prefilled_outputs"]["mutable"].append("generation-owned")
            if self.action:
                return self.action(kwargs)
            for value in (0.1, 0.2, 0.3):
                if kwargs["stop_check_fn"]():
                    break
                kwargs["audio_streamer"].put(
                    [Tensor(Vector([value, -value]))], [SimpleNamespace(item=lambda: 0)]
                )
            kwargs["audio_streamer"].end()
            calls.append(("native-returned",))
            return SimpleNamespace(reach_max_step_sample=SimpleNamespace(any=lambda: False))

    model = Model()

    def construct_model(path, **kwargs):
        calls.append(("model", path, kwargs))
        return model

    def construct_tokenizer(path, **kwargs):
        calls.append(("tokenizer", path, kwargs))
        return "local-tokenizer"

    class Processor:
        def __init__(self, **kwargs):
            calls.append(("processor", kwargs))
            self.tokenizer = kwargs["tokenizer"]
            self.width = 4

        def process_input_with_cached_prompt(self, **kwargs):
            calls.append(("input", kwargs))
            return {
                "input_ids": Tokens(10),
                "tts_lm_input_ids": Tokens(12),
                "tts_text_ids": Tokens(self.width),
            }

    @contextmanager
    def safe_globals(values):
        calls.append(("safe_globals", values))
        yield

    def load(path, **kwargs):
        calls.append(("load_prompt", path, kwargs))
        return {"mutable": []}

    torch = SimpleNamespace(
        float32="float32",
        set_num_threads=lambda value: calls.append(("threads", value)),
        get_num_interop_threads=lambda: 2,
        set_num_interop_threads=lambda value: calls.append(("interop", value)),
        serialization=SimpleNamespace(safe_globals=safe_globals),
        load=load,
    )
    modules = SimpleNamespace(
        torch=torch,
        Model=SimpleNamespace(from_pretrained=construct_model),
        Tokenizer=SimpleNamespace(from_pretrained=construct_tokenizer),
        Processor=Processor,
        AudioProcessor=lambda **kwargs: (
            calls.append(("audio_processor", kwargs)) or "audio-processor"
        ),
        ModelOutput="official-output-class",
        DynamicCache="official-cache-class",
    )
    monkeypatch.setattr(vv, "_verify_runtime", lambda manifest: calls.append(("runtime-verified",)))
    monkeypatch.setattr(vv, "_runtime_modules", lambda: modules)
    return calls, model


def loaded(tmp_path, monkeypatch):
    root, _ = assets(tmp_path, monkeypatch)
    calls, model = fake_runtime(monkeypatch)
    backend = vv.VibeVoiceBackend(root, vv.VIBEVOICE_VOICE)
    backend.load()
    return backend, calls, model


def test_module_and_selected_factory_do_not_import_optional_runtime():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from packages.providers.tts_backends import create_tts_backend
from packages.providers.vibevoice_backend import VibeVoiceBackend
create_tts_backend('VibeVoice Realtime 0.5B', '/synthetic/missing')
assert not {'torch','numpy','transformers','diffusers','vibevoice'} & sys.modules.keys()
""",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_checked_in_manifest_pins_assets_and_native_runtime():
    manifest = vv._manifest()
    assert (
        manifest["files"]["checkpoint/model.safetensors"]["sha256"]
        == "7758b150b8139deb48ac1ff6f181f745c8fedd5511232fd974b3eb217d83b514"
    )
    assert (
        manifest["files"]["voices/en-Carter_man.pt"]["sha256"]
        == "a7bfdf1cd4939c22469bcfc6f427ae9c4467b3df46c2c14303a39c294cfc6897"
    )
    assert (
        "vibevoice/modular/modeling_vibevoice_streaming_inference.py" in manifest["runtime_files"]
    )


@pytest.mark.parametrize(
    "failure", ["hash", "size", "missing", "root_symlink", "directory_symlink", "file_symlink"]
)
def test_bad_assets_fail_before_any_runtime_import(tmp_path, monkeypatch, failure):
    root, _ = assets(tmp_path, monkeypatch)
    calls, _ = fake_runtime(monkeypatch)
    target = root / "voices/en-Carter_man.pt"
    if failure == "hash":
        value = target.read_bytes()
        target.write_bytes(bytes([value[0] ^ 1]) + value[1:])
    elif failure == "size":
        target.write_bytes(b"short")
    elif failure == "missing":
        target.unlink()
    elif failure == "root_symlink":
        original = root
        root = tmp_path / "linked"
        root.symlink_to(original, target_is_directory=True)
    elif failure == "directory_symlink":
        (root / "voices").rename(tmp_path / "actual-voices")
        (root / "voices").symlink_to(tmp_path / "actual-voices", target_is_directory=True)
    else:
        target.rename(tmp_path / "actual-preset")
        target.symlink_to(tmp_path / "actual-preset")
    backend = vv.VibeVoiceBackend(root, vv.VIBEVOICE_VOICE)
    with pytest.raises(
        vv.TTSBackendError, match="^Pinned VibeVoice CPU assets or runtime are unavailable$"
    ):
        backend.load()
    assert calls == [] and backend.assets_state == "not_checked"


@pytest.mark.parametrize(
    "key", ["revision", "runtime_revision", "tokenizer_revision", "voice", "runtime_versions"]
)
def test_manifest_identity_cannot_be_changed(tmp_path, monkeypatch, key):
    root, path = assets(tmp_path, monkeypatch)
    value = json.loads(path.read_text())
    value[key] = "untrusted"
    path.write_text(json.dumps(value))
    calls, _ = fake_runtime(monkeypatch)
    with pytest.raises(vv.TTSBackendError):
        vv.VibeVoiceBackend(root, vv.VIBEVOICE_VOICE).load()
    assert calls == []


@pytest.mark.parametrize("failure", ["version", "shadow", "hash", "extra_module", "symlink"])
def test_runtime_distribution_and_resolved_implementation_are_checked(
    tmp_path, monkeypatch, failure
):
    root, path = assets(tmp_path, monkeypatch)
    manifest = json.loads(path.read_text())
    source = root / "vibevoice/__init__.py"
    source.parent.mkdir()
    data = (root / "checkpoint/README.md").read_bytes()
    source.write_bytes(data)
    monkeypatch.setattr(
        vv.importlib.metadata,
        "version",
        lambda name: "wrong" if failure == "version" else manifest["runtime_versions"][name],
    )
    package_files = [Path("vibevoice/__init__.py")]
    if failure == "extra_module":
        package_files.append(Path("vibevoice/unreviewed.py"))
    monkeypatch.setattr(
        vv.importlib.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            files=package_files, locate_file=lambda relative: root / relative
        ),
    )
    monkeypatch.setattr(
        vv.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(
            origin="/shadow/vibevoice.py" if failure == "shadow" else str(source)
        ),
    )
    if failure == "hash":
        source.write_bytes(b"modified" + data[8:])
    elif failure == "symlink":
        source.rename(root / "actual-runtime.py")
        source.symlink_to(root / "actual-runtime.py")
    with pytest.raises((vv.TTSBackendError, OSError)):
        vv._verify_runtime(manifest)


def test_load_is_explicit_offline_cpu_and_uses_safe_public_preset(tmp_path, monkeypatch):
    backend, calls, _ = loaded(tmp_path, monkeypatch)
    assert backend.assets_state == "verified"
    assert ("threads", 4) in calls and ("interop", 1) in calls
    assert ("safe_globals", ["official-output-class", "official-cache-class"]) in calls
    model = next(call for call in calls if call[0] == "model")
    assert model[2] == dict(
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=True,
        torch_dtype="float32",
        device_map="cpu",
        attn_implementation="sdpa",
    )
    tokenizer = next(call for call in calls if call[0] == "tokenizer")
    assert tokenizer[1] == str(backend.model_dir / "tokenizer")
    assert tokenizer[2] == dict(local_files_only=True, trust_remote_code=False)
    assert next(call for call in calls if call[0] == "load_prompt")[2] == dict(
        map_location="cpu", weights_only=True
    )
    assert ("steps", {"num_steps": 5}) in calls
    before = len(calls)
    backend.load()
    assert len(calls) == before


def test_unexpected_gpu_parameter_is_refused(tmp_path, monkeypatch):
    root, _ = assets(tmp_path, monkeypatch)
    fake_runtime(monkeypatch, device="cuda")
    backend = vv.VibeVoiceBackend(root, vv.VIBEVOICE_VOICE)
    with pytest.raises(vv.TTSBackendError):
        backend.load()
    assert backend.assets_state == "not_checked"


def test_native_chunks_emit_on_callers_thread_and_preserve_prompt(tmp_path, monkeypatch):
    backend, calls, _ = loaded(tmp_path, monkeypatch)
    output = []
    owner = threading.get_ident()
    backend.produce("Public synthetic reply.", output.append, threading.Event())
    assert [item.sample_rate for item in output] == [24000] * 3
    assert [list(item.samples) for item in output] == [[0.1, -0.1], [0.2, -0.2], [0.3, -0.3]]
    assert backend.prompt == {"mutable": []}
    generate = next(call for call in calls if call[0] == "generate")
    assert generate[2] == owner
    assert generate[1]["max_new_tokens"] == 904
    assert generate[1]["return_speech"] is False
    assert ("native-returned",) in calls


def test_callback_failure_unwinds_native_generation_instead_of_eof(tmp_path, monkeypatch):
    backend, calls, _ = loaded(tmp_path, monkeypatch)
    failure = RuntimeError("synthetic callback cancellation")

    def emit(waveform):
        raise failure

    with pytest.raises(RuntimeError) as error:
        backend.produce("Public synthetic reply.", emit, threading.Event())
    assert error.value is failure
    assert ("native-returned",) not in calls


def test_cancel_after_first_chunk_finishes_native_call_before_return(tmp_path, monkeypatch):
    backend, calls, _ = loaded(tmp_path, monkeypatch)
    cancelled = threading.Event()
    output = []

    def emit(waveform):
        output.append(waveform)
        cancelled.set()

    with pytest.raises(vv.TTSBackendError, match="cancelled"):
        backend.produce("Public synthetic reply.", emit, cancelled)
    assert len(output) == 1 and ("native-returned",) in calls


@pytest.mark.parametrize("text", ["", " ", "x" * 1201, None])
def test_input_bounds_are_checked_before_native_generation(tmp_path, monkeypatch, text):
    backend, calls, _ = loaded(tmp_path, monkeypatch)
    with pytest.raises(vv.TTSBackendError):
        backend.produce(text, lambda value: None, threading.Event())
    assert not any(call[0] == "generate" for call in calls)


def test_token_context_and_truncated_native_completion_are_errors(tmp_path, monkeypatch):
    backend, calls, model = loaded(tmp_path, monkeypatch)
    backend.processor.width = 8190
    with pytest.raises(vv.TTSBackendError, match="context"):
        backend.produce("Public synthetic reply.", lambda value: None, threading.Event())
    assert not any(call[0] == "generate" for call in calls)
    backend.processor.width = 4
    model.action = lambda kwargs: SimpleNamespace(
        reach_max_step_sample=SimpleNamespace(any=lambda: True)
    )
    with pytest.raises(vv.TTSBackendError, match="context"):
        backend.produce("Public synthetic reply.", lambda value: None, threading.Event())


def test_pre_cancelled_request_never_enters_native_generation(tmp_path, monkeypatch):
    backend, calls, _ = loaded(tmp_path, monkeypatch)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(vv.TTSBackendError, match="cancelled"):
        backend.produce("Public synthetic reply.", lambda value: None, cancelled)
    assert not any(call[0] == "generate" for call in calls)


def test_native_error_is_not_reported_as_successful_stream_end(tmp_path, monkeypatch):
    backend, calls, model = loaded(tmp_path, monkeypatch)
    failure = RuntimeError("synthetic native failure")

    def fail(kwargs):
        raise failure

    model.action = fail
    with pytest.raises(RuntimeError) as error:
        backend.produce("Public synthetic reply.", lambda value: None, threading.Event())
    assert error.value is failure
    assert ("native-returned",) not in calls


@pytest.mark.parametrize("kind", ["stereo", "empty", "duration", "batch"])
def test_invalid_native_chunk_is_not_emitted(tmp_path, monkeypatch, kind):
    backend, _, model = loaded(tmp_path, monkeypatch)
    output = []

    class Stereo(Vector):
        @property
        def shape(self):
            return (2, 2)

    class OverLimit(Vector):
        def __len__(self):
            return 120 * 24000 + 1

    def invalid(kwargs):
        values = Stereo([0.1, 0.2]) if kind == "stereo" else Vector([])
        if kind == "duration":
            values = OverLimit()
        chunks = [Tensor(values)] * (2 if kind == "batch" else 1)
        kwargs["audio_streamer"].put(chunks, [SimpleNamespace(item=lambda: 0)])

    model.action = invalid
    with pytest.raises(vv.TTSBackendError):
        backend.produce("Public synthetic reply.", output.append, threading.Event())
    assert output == []
