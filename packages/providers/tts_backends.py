"""Allowlisted local CPU TTS adapters; optional inference imports stay lazy.

No model installer, remote adapter, user-provided Python module or execution
configuration is accepted here. Provider computation may finish after caller
cancellation; the worker fences all subsequent inference and output separately.
"""

import hashlib
import importlib.metadata
import json
import math
import numbers
import os
import stat
from array import array
from dataclasses import dataclass
from pathlib import Path

SUPERTONIC_REVISION = "aafc6e32416a594460b32413efc49d7fe4ce6d46"
SUPERTONIC_SDK_VERSION = "1.3.1"
SUPERTONIC_ORT_VERSION = "1.23.1"
VIBEVOICE_REVISION = "6bce5f06044837fe6d2c5d7a71a84f0416bd57e4"
VIBEVOICE_VOICE = "en-Carter_man"
SUPPORTED_TTS_PROFILES = ("Kokoro 82M", "Supertonic 3", "VibeVoice Realtime 0.5B")
SUPERTONIC_VOICES = frozenset(f"{gender}{index}" for gender in ("F", "M") for index in range(1, 6))
KOKORO_VOICES = frozenset({"af_heart"})
MAX_AUDIO_SECONDS = 120
MAX_FRAME_SECONDS = 1
_MANIFEST = Path(__file__).with_name("supertonic3_manifest.json")


class TTSBackendError(RuntimeError):
    """A safe boundary error: never expose model paths, text or runtime output."""


@dataclass(frozen=True)
class TTSProfile:
    voice: str
    voices: frozenset[str]
    mode: str
    input_limit: int
    artifact_revision: str | None = None
    requires_model_directory: bool = False


_PROFILES = {
    "Kokoro 82M": TTSProfile("af_heart", KOKORO_VOICES, "sentence_buffered", 16000),
    "Supertonic 3": TTSProfile(
        "F1", SUPERTONIC_VOICES, "buffered", 1200, SUPERTONIC_REVISION, True
    ),
    "VibeVoice Realtime 0.5B": TTSProfile(
        VIBEVOICE_VOICE,
        frozenset({VIBEVOICE_VOICE}),
        "native_streaming",
        1200,
        VIBEVOICE_REVISION,
        True,
    ),
}


def profile_settings(profile):
    if not isinstance(profile, str) or profile not in _PROFILES:
        raise ValueError("Unsupported configured TTS profile")
    return _PROFILES[profile]


def default_voice(profile):
    return profile_settings(profile).voice


def validate_voice(profile, voice=None):
    settings = profile_settings(profile)
    resolved = settings.voice if voice is None else voice
    if not isinstance(resolved, str) or resolved not in settings.voices:
        raise ValueError("Unsupported configured TTS voice")
    return resolved


def synthesis_mode(profile):
    return profile_settings(profile).mode


def artifact_revision(profile):
    return profile_settings(profile).artifact_revision


def input_limit(profile):
    return profile_settings(profile).input_limit


@dataclass(frozen=True)
class AudioWaveform:
    samples: object
    sample_rate: int


def produce_synthesis(backend, text, emit, cancelled):
    """Run one producer to completion while its caller owns model execution.

    Native streaming adapters implement blocking ``produce(text, emit, cancelled)``.
    ``emit`` is synchronous and may block for bounded consumer backpressure; it
    must stop when cancellation is set. Adapters return only after native work
    has ended. Existing synchronous generators stop at waveform boundaries;
    cancelling a request cannot preempt their current native kernel.
    """
    if cancelled.is_set():
        return
    producer = getattr(backend, "produce", None)
    if producer is not None:
        producer(text, emit, cancelled)
        return
    iterator = iter(backend.synthesize(text))
    try:
        while not cancelled.is_set():
            waveform = next(iterator, None)
            if waveform is None or cancelled.is_set():
                return
            emit(waveform)
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()


def pcm_frames(waveform, *, remaining_seconds=MAX_AUDIO_SECONDS):
    """Validate one actual mono waveform completely before exposing its frames.

    Accept a vector or one-item batch, never flatten multiple channels/batches.
    Keep actual padded/inter-phrase samples; predicted duration is not alignment.
    Conversion needs no numpy import in the core/Agent environment.
    """
    if not isinstance(waveform, AudioWaveform):
        raise TTSBackendError("Invalid voice waveform")
    rate = waveform.sample_rate
    if type(rate) is not int or not 8000 <= rate <= 96000:
        raise TTSBackendError("Invalid voice sample rate")
    samples = waveform.samples
    shape = getattr(samples, "shape", None)
    if shape is not None:
        if len(shape) == 2 and shape[0] == 1:
            samples = samples[0]
        elif len(shape) != 1:
            raise TTSBackendError("Voice waveform must be mono")
    elif (
        isinstance(samples, (list, tuple))
        and samples
        and isinstance(samples[0], (list, tuple, array))
    ):
        if len(samples) != 1:
            raise TTSBackendError("Voice waveform must be mono")
        samples = samples[0]
    try:
        count = len(samples)
    except (TypeError, OverflowError) as exc:
        raise TTSBackendError("Invalid voice waveform") from exc
    if count < 1 or count > rate * remaining_seconds:
        raise TTSBackendError("Voice audio exceeds bounded duration")
    frames, values = [], array("h")
    for value in samples:
        if (
            isinstance(value, bool)
            or not isinstance(value, numbers.Real)
            or not math.isfinite(value)
        ):
            raise TTSBackendError("Invalid voice sample")
        values.append(int(max(-1.0, min(1.0, value)) * 32767))
        if len(values) == rate:
            if os.sys.byteorder != "little":
                values.byteswap()
            frames.append(values.tobytes())
            values = array("h")
    if values:
        if os.sys.byteorder != "little":
            values.byteswap()
        frames.append(values.tobytes())
    if sum(len(frame) // 2 for frame in frames) != count:
        raise TTSBackendError("Invalid voice waveform length")
    return frames, count, rate


def _verify_assets(model_dir, voice):
    """Hash exact public artifacts through no-follow descriptors before loading.

    Owner configuration keeps model assets outside Agent-writable roots. This
    validator does not follow symlinks or accept an owner-supplied manifest.
    """
    if model_dir is None:
        raise TTSBackendError("Pinned local voice assets are required")
    manifest = json.loads(_MANIFEST.read_text())
    if manifest.get("revision") != SUPERTONIC_REVISION:
        raise TTSBackendError("Invalid pinned voice manifest")
    records = manifest["files"]
    required = {
        "LICENSE",
        "onnx/tts.json",
        "onnx/unicode_indexer.json",
        "onnx/duration_predictor.onnx",
        "onnx/text_encoder.onnx",
        "onnx/vector_estimator.onnx",
        "onnx/vocoder.onnx",
        f"voice_styles/{voice}.json",
    }
    if not required <= records.keys():
        raise TTSBackendError("Incomplete pinned voice manifest")
    root_fd = os.open(model_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for relative in sorted(required):
            record = records[relative]
            parts = relative.split("/")
            parent_fd = os.dup(root_fd)
            try:
                for part in parts[:-1]:
                    next_fd = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
                    )
                    os.close(parent_fd)
                    parent_fd = next_fd
                descriptor = os.open(
                    parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
                )
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode) or info.st_size != record["size_bytes"]:
                        raise TTSBackendError("Pinned voice asset is invalid")
                    digest = hashlib.sha256()
                    with os.fdopen(descriptor, "rb", closefd=False) as source:
                        for chunk in iter(lambda: source.read(1 << 20), b""):
                            digest.update(chunk)
                    if digest.hexdigest() != record["sha256"]:
                        raise TTSBackendError("Pinned voice asset is invalid")
                finally:
                    os.close(descriptor)
            finally:
                os.close(parent_fd)
    finally:
        os.close(root_fd)


class _KokoroBackend:
    assets_state = "not_checked"

    def __init__(self, voice):
        self.voice = voice
        self.pipeline = None

    def load(self):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from kokoro import KPipeline

        self.pipeline = KPipeline(lang_code="a", device="cpu", repo_id="hexgrad/Kokoro-82M")

    def synthesize(self, text):
        for _, _, audio in self.pipeline(text, voice=self.voice):
            yield AudioWaveform(audio.numpy() if hasattr(audio, "numpy") else audio, 24000)


class _SupertonicBackend:
    def __init__(self, model_dir, voice):
        self.model_dir, self.voice = model_dir, voice
        self.assets_state = "not_checked"
        self.pipeline = self.style = None

    def load(self):
        _verify_assets(self.model_dir, self.voice)
        if (
            importlib.metadata.version("supertonic") != SUPERTONIC_SDK_VERSION
            or importlib.metadata.version("onnxruntime") != SUPERTONIC_ORT_VERSION
        ):
            raise TTSBackendError("The pinned voice runtime is required")
        from supertonic import TTS, loader

        # The pinned SDK exposes the provider list through its loader constant,
        # not an inference HTTP option. Also verify every constructed session.
        loader.DEFAULT_ONNX_PROVIDERS = ["CPUExecutionProvider"]
        self.pipeline = TTS(
            model="supertonic-3",
            model_dir=str(self.model_dir),
            auto_download=False,
            intra_op_num_threads=4,
            inter_op_num_threads=1,
        )
        if self.pipeline.sample_rate != 44100:
            raise TTSBackendError("Unexpected pinned voice sample rate")
        for name in ("dp_ort", "text_enc_ort", "vector_est_ort", "vocoder_ort"):
            if getattr(self.pipeline.model, name).get_providers() != ["CPUExecutionProvider"]:
                raise TTSBackendError("The configured voice runtime must use CPU only")
        self.style = self.pipeline.get_voice_style(self.voice)
        self.assets_state = "verified"

    def synthesize(self, text):
        waveform, _predicted_duration = self.pipeline.synthesize(
            text=text,
            voice_style=self.style,
            total_steps=8,
            speed=1.05,
            lang="en",
        )
        yield AudioWaveform(waveform, self.pipeline.sample_rate)


def create_tts_backend(profile, model_dir=None, voice=None):
    voice = validate_voice(profile, voice)
    if profile == "Kokoro 82M":
        return _KokoroBackend(voice)
    if profile == "VibeVoice Realtime 0.5B":
        from packages.providers.vibevoice_backend import VibeVoiceBackend

        return VibeVoiceBackend(Path(model_dir) if model_dir is not None else None, voice)
    return _SupertonicBackend(Path(model_dir) if model_dir is not None else None, voice)
