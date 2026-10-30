"""Lazy GPU Whisper over pinned local checkpoints and transient, bounded audio.

The voice worker owns admission and serialization. This adapter starts no thread,
downloads nothing, and never substitutes CPU inference for the requested GPU.
"""

import hashlib
import importlib
import io
import os
import re
import stat
from fractions import Fraction
from pathlib import Path

SAMPLE_RATE = 16000
MAX_SECONDS = 60
MAX_SAMPLES = SAMPLE_RATE * MAX_SECONDS
MAX_AUDIO_BYTES = 10_000_000
MAX_CHECKPOINT_BYTES = 100_000_000
MAX_TEXT_CHARS = 16000

# Official OpenAI Whisper checkpoint identities, also recorded in its loader:
# https://github.com/openai/whisper/blob/v20250625/whisper/__init__.py
_OFFICIAL_SHA256 = {
    "tiny.en": "d3dd57d32accea0b295c96e26691aa14d8822fac7d9d27d5dc00b4ca2826dd03",
    "tiny": "65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9",
}


class STTBackendError(RuntimeError):
    """Pinned runtime, checkpoint, GPU placement, or inference is unavailable."""


class STTInputError(STTBackendError):
    """The transient audio is malformed, empty, or exceeds its decoded bound."""


def _decode_audio(data):
    if not isinstance(data, bytes) or not data or len(data) > MAX_AUDIO_BYTES:
        raise STTInputError("Invalid bounded audio upload")
    try:
        av = importlib.import_module("av")
        np = importlib.import_module("numpy")
    except ImportError as exc:
        raise STTBackendError("Local audio decoding runtime is unavailable") from exc

    pcm = io.BytesIO()
    samples = 0
    duration = Fraction(0)
    try:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)

        def append(frames):
            nonlocal samples
            for frame in frames:
                count = frame.samples
                if type(count) is not int or count < 0 or samples + count > MAX_SAMPLES:
                    raise STTInputError("Decoded audio exceeds 60 seconds")
                raw = frame.to_ndarray().tobytes()
                if len(raw) != count * 2:
                    raise STTInputError("Invalid resampled mono audio")
                pcm.write(raw)
                samples += count

        # The upload is already an in-memory stream. Deny nested file/network
        # protocols so a playlist-like upload cannot fetch external resources.
        with av.open(
            io.BytesIO(data),
            mode="r",
            metadata_errors="ignore",
            options={"protocol_whitelist": ""},
        ) as container:
            for frame in container.decode(audio=0):
                count, rate = frame.samples, frame.sample_rate
                if type(count) is not int or count < 0 or type(rate) is not int or rate <= 0:
                    raise STTInputError("Invalid decoded audio timing")
                duration += Fraction(count, rate)
                if duration > MAX_SECONDS:
                    raise STTInputError("Decoded audio exceeds 60 seconds")
                frame.pts = None
                append(resampler.resample(frame))
            append(resampler.resample(None))
        if samples == 0:
            raise STTInputError("Decoded audio is empty")
        return np.frombuffer(pcm.getbuffer(), dtype=np.int16).astype(np.float32) / 32768.0
    except STTInputError:
        raise
    except Exception as exc:
        raise STTInputError("Audio could not be decoded within its bounds") from exc


class OpenAIWhisperBackend:
    def __init__(self, checkpoint, *, model="tiny.en", device="cuda"):
        if model not in _OFFICIAL_SHA256:
            raise ValueError("Only pinned official tiny and tiny.en checkpoints are supported")
        if not isinstance(device, str) or not re.fullmatch(r"cuda(?::[0-9]+)?", device):
            raise ValueError("Whisper GPU inference requires an explicit CUDA device")
        self.checkpoint = Path(checkpoint)
        if not self.checkpoint.is_absolute():
            raise ValueError("Whisper requires an explicit absolute local checkpoint path")
        self.model = model
        self.requested_device = f"cuda:{int(device.partition(':')[2] or '0')}"
        self._model = None
        self._loaded_device = None

    @property
    def loaded_device(self):
        return self._loaded_device

    @property
    def device(self):
        """Actual verified placement, rather than the configured target."""
        return self._loaded_device

    def _checkpoint_bytes(self):
        try:
            descriptor = os.open(self.checkpoint, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_CHECKPOINT_BYTES:
                    raise STTBackendError("Invalid pinned Whisper checkpoint")
                data = source.read(MAX_CHECKPOINT_BYTES + 1)
            if (
                len(data) > MAX_CHECKPOINT_BYTES
                or hashlib.sha256(data).hexdigest() != _OFFICIAL_SHA256[self.model]
            ):
                raise STTBackendError("Pinned Whisper checkpoint identity does not match")
            return data
        except STTBackendError:
            raise
        except OSError as exc:
            raise STTBackendError("Pinned local Whisper checkpoint is unavailable") from exc

    def _placement(self, model):
        try:
            parameter_devices = {str(parameter.device) for parameter in model.parameters()}
            devices = parameter_devices | {str(buffer.device) for buffer in model.buffers()}
        except Exception as exc:
            raise STTBackendError("Whisper GPU placement could not be verified") from exc
        if not parameter_devices or devices != {self.requested_device}:
            raise STTBackendError("Whisper model is not wholly on the requested GPU")
        return next(iter(devices))

    def _load(self):
        checkpoint = self._checkpoint_bytes()
        try:
            torch = importlib.import_module("torch")
            if not torch.cuda.is_available():
                raise STTBackendError("Whisper GPU runtime is unavailable")
            whisper = importlib.import_module("whisper")
            # Hash and load the same immutable bytes: reopening a checked path
            # would let replacement or in-place mutation bypass its identity.
            state = torch.load(io.BytesIO(checkpoint), map_location="cpu", weights_only=True)
            dimensions = whisper.ModelDimensions(**state["dims"])
            model = whisper.Whisper(dimensions)
            model.load_state_dict(state["model_state_dict"])
            model = model.to(self.requested_device)
            model.eval()
            placement = self._placement(model)
        except STTBackendError:
            raise
        except Exception as exc:
            raise STTBackendError("Pinned Whisper GPU model could not load") from exc
        self._model = model
        self._loaded_device = placement

    def transcribe(self, data: bytes):
        audio = _decode_audio(data)
        if self._model is None:
            self._load()
        self._loaded_device = None
        self._loaded_device = self._placement(self._model)
        try:
            result = self._model.transcribe(
                audio,
                language="en" if self.model == "tiny.en" else None,
                task="transcribe",
                fp16=True,
                temperature=0,
                beam_size=1,
                condition_on_previous_text=False,
                word_timestamps=False,
                without_timestamps=True,
                verbose=None,
            )
        except Exception as exc:
            raise STTBackendError("Whisper GPU transcription failed") from exc
        self._loaded_device = None
        self._loaded_device = self._placement(self._model)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("text"), str)
            or len(result["text"]) > MAX_TEXT_CHARS
            or not isinstance(result.get("language"), str)
            or not re.fullmatch(r"[a-z]{2,3}", result["language"])
        ):
            raise STTBackendError("Whisper returned an invalid bounded transcript")
        return {
            "text": result["text"].strip(),
            "language": result["language"],
            "final": True,
            "provider": "openai-whisper",
            "model": self.model,
            "device": self._loaded_device,
        }
