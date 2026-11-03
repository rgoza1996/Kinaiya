"""Local Ming 3 Tiny buffered GPU TTS adapter with pinned asset and runtime verification."""

import errno
import hashlib
import io
import json
import math
import os
import stat
import struct
import wave
from pathlib import Path
from typing import Any

from packages.providers.tts_backends import AudioWaveform, TTSBackendError

_MANIFEST_PATH = Path(__file__).with_name("ming3_manifest.json")
MANIFEST = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
MANIFEST_SHA256 = hashlib.sha256(_MANIFEST_PATH.read_bytes()).hexdigest()

MING3_PROFILE = "Ming 3 Tiny"
MING3_REPOSITORY = MANIFEST["repository"]
MING3_REVISION = MANIFEST["revision"]
MING3_RUNTIME_REPOSITORY = MANIFEST["runtime_repository"]
MING3_RUNTIME_REVISION = MANIFEST["runtime_revision"]
MING3_VOICE = MANIFEST["default_voice"]
MING3_VOICES = tuple(MANIFEST["supported_voices"])
MING3_SAMPLE_RATE = int(MANIFEST["sample_rate"])
MING3_MAX_CHARS = int(MANIFEST["max_input_chars"])
MING3_MAX_SAMPLES = int(MANIFEST["max_samples"])
MING3_TASK_TYPE = MANIFEST["task_type"]

_MAX_SAFETENSORS_HEADER = 8 * 1024 * 1024
_ALLOWED_DTYPES = {"BF16", "F16", "F32", "I32", "I64", "BOOL", "U8"}


class SynthesisCancelled(TTSBackendError):
    """Raised when caller cancellation is observed before or during Ming 3 Tiny synthesis."""


def _open_nofollow(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("Ming 3 Tiny asset path cannot be a symlink") from exc
        raise ValueError("Ming 3 Tiny asset is missing or unreadable") from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise ValueError("Ming 3 Tiny asset must be a regular file")
    return fd


def _open_directory_nofollow(path: Path) -> int:
    try:
        st = path.lstat()
    except OSError as exc:
        raise ValueError("Ming 3 Tiny model directory does not exist") from exc
    if stat.S_ISLNK(st.st_mode):
        raise ValueError("Ming 3 Tiny model directory cannot be a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError("Ming 3 Tiny model directory is not a directory")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError("Ming 3 Tiny model directory cannot be a symlink") from exc
        raise ValueError("Ming 3 Tiny model directory does not exist") from exc
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        os.close(fd)
        raise ValueError("Ming 3 Tiny model directory is not a directory")
    return fd


def _verify_file(path: Path, expected_sha256: str, expected_size: int | None = None) -> bytes:
    fd = _open_nofollow(path)
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        while chunk := os.read(fd, 1024 * 1024):
            total += len(chunk)
            if expected_size is not None and total > expected_size:
                raise ValueError(f"Ming 3 Tiny asset size mismatch for {path.name}")
            digest.update(chunk)
            if expected_size is None or expected_size <= 64 * 1024:
                chunks.append(chunk)
    finally:
        os.close(fd)
    if expected_size is not None and total != expected_size:
        raise ValueError(f"Ming 3 Tiny asset size mismatch for {path.name}")
    if digest.hexdigest() != expected_sha256:
        raise ValueError(f"Ming 3 Tiny asset digest mismatch for {path.name}")
    return b"".join(chunks)


def _verify_safetensors_header(path: Path, file_size: int) -> None:
    if file_size < 8:
        raise ValueError("Ming 3 Tiny safetensors file is truncated")
    fd = _open_nofollow(path)
    try:
        prefix = os.read(fd, 8)
        if len(prefix) != 8:
            raise ValueError("Ming 3 Tiny safetensors header is truncated")
        header_len = struct.unpack("<Q", prefix)[0]
        if header_len <= 2 or header_len > _MAX_SAFETENSORS_HEADER or 8 + header_len > file_size:
            raise ValueError("Ming 3 Tiny safetensors header length is invalid")
        raw_header = os.read(fd, header_len)
        if len(raw_header) != header_len:
            raise ValueError("Ming 3 Tiny safetensors header could not be read")
    finally:
        os.close(fd)
    try:
        header = json.loads(raw_header.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Ming 3 Tiny safetensors header is not valid JSON") from exc
    if not isinstance(header, dict):
        raise ValueError("Ming 3 Tiny safetensors header must be a JSON object")
    data_limit = file_size - 8 - header_len
    tensor_count = 0
    for key, entry in header.items():
        if key == "__metadata__":
            if not isinstance(entry, dict):
                raise ValueError("Ming 3 Tiny safetensors metadata must be an object")
            continue
        if not isinstance(entry, dict):
            raise ValueError("Ming 3 Tiny safetensors tensor entry must be an object")
        dtype = entry.get("dtype")
        shape = entry.get("shape")
        offsets = entry.get("data_offsets")
        if (
            dtype not in _ALLOWED_DTYPES
            or not isinstance(shape, list)
            or any(type(dim) is not int or dim < 0 for dim in shape)
            or not isinstance(offsets, list)
            or len(offsets) != 2
            or any(type(pos) is not int for pos in offsets)
        ):
            raise ValueError("Ming 3 Tiny safetensors tensor descriptor is invalid")
        start, end = offsets
        if start < 0 or end < start or end > data_limit:
            raise ValueError("Ming 3 Tiny safetensors tensor offsets are out of bounds")
        tensor_count += 1
    if tensor_count == 0:
        raise ValueError("Ming 3 Tiny safetensors must contain at least one tensor")


def _verify_config_no_remote_code(raw_config: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_config.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Ming 3 Tiny config.json is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Ming 3 Tiny config.json must be a JSON object")
    if "auto_map" in parsed or parsed.get("trust_remote_code") is True:
        raise ValueError("Ming 3 Tiny rejects remote code execution hooks in config.json")
    return parsed


def verify_ming3_assets(model_dir: str | Path | None, manifest: dict[str, Any] = MANIFEST) -> Path:
    if model_dir is None:
        raise ValueError("Ming 3 Tiny model directory must be an absolute path")
    raw = str(model_dir)
    if not raw or any(ch in raw for ch in "\0\r\n"):
        raise ValueError("Ming 3 Tiny model directory must be a clean absolute path")
    root = Path(raw)
    if not root.is_absolute():
        raise ValueError("Ming 3 Tiny model directory must be an absolute path")
    dir_fd = _open_directory_nofollow(root)
    os.close(dir_fd)

    preset_dir = root / "preset_speakers"
    preset_fd = _open_directory_nofollow(preset_dir)
    os.close(preset_fd)

    license_path = root / manifest["license_file"]
    _verify_file(license_path, manifest["license_sha256"])

    for asset in manifest["assets"]:
        rel_path = asset["path"]
        target = root / rel_path
        raw_bytes = _verify_file(target, asset["sha256"], asset["size"])
        if rel_path == "config.json":
            _verify_config_no_remote_code(raw_bytes)
        elif rel_path.endswith(".safetensors"):
            _verify_safetensors_header(target, asset["size"])
    return root


def validate_ming3_request(
    text: str,
    *,
    voice: str = MING3_VOICE,
    task_type: str = MING3_TASK_TYPE,
    emotion: str | None = None,
    voice_description: str | None = None,
) -> tuple[str, str]:
    if not isinstance(text, str):
        raise ValueError("Ming 3 Tiny synthesis text must be a string")
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("Ming 3 Tiny synthesis text must not be empty")
    if len(cleaned) > MING3_MAX_CHARS:
        raise ValueError(f"Ming 3 Tiny synthesis text exceeds {MING3_MAX_CHARS} characters")
    if voice not in MING3_VOICES:
        raise ValueError(f"Ming 3 Tiny requires configured voice {MING3_VOICE}")
    if task_type != MING3_TASK_TYPE:
        raise ValueError(f"Ming 3 Tiny requires preset speaker task '{MING3_TASK_TYPE}'")
    if emotion is not None or voice_description is not None:
        raise ValueError(
            "Ming 3 Tiny ip preset task does not accept emotion or freeform voice descriptions"
        )
    return cleaned, voice


def _verify_cuda_residency(model: Any) -> str:
    params_fn = getattr(model, "parameters", None)
    buffers_fn = getattr(model, "buffers", None)
    tensors = []
    if callable(params_fn):
        tensors.extend(list(params_fn()))
    if callable(buffers_fn):
        tensors.extend(list(buffers_fn()))
    if not tensors:
        raise TTSBackendError("Ming 3 Tiny model exposes no CUDA parameters or buffers")
    devices = set()
    for tensor in tensors:
        if not getattr(tensor, "is_cuda", False):
            raise TTSBackendError("Ming 3 Tiny model tensor is not resident on CUDA")
        devices.add(str(getattr(tensor, "device", "")))
    if devices != {"cuda:0"}:
        raise TTSBackendError("Ming 3 Tiny model tensors must reside on cuda:0")
    return "cuda:0"


def _validate_samples(samples: list[float] | tuple[float, ...]) -> list[float]:
    if not samples:
        raise TTSBackendError("Ming 3 Tiny returned empty audio samples")
    if len(samples) > MING3_MAX_SAMPLES:
        raise TTSBackendError(
            f"Ming 3 Tiny output exceeded maximum sample cap ({MING3_MAX_SAMPLES} samples)"
        )
    validated: list[float] = []
    non_zero = False
    for value in samples:
        fval = float(value)
        if not math.isfinite(fval):
            raise TTSBackendError("Ming 3 Tiny produced non-finite audio sample")
        clamped = max(-1.0, min(1.0, fval))
        if int(round(clamped * 32767.0)) != 0:
            non_zero = True
        validated.append(clamped)
    if not non_zero:
        raise TTSBackendError("Ming 3 Tiny produced all-zero silent audio")
    return validated


def _pcm16_wav_from_samples(samples: list[float] | tuple[float, ...], sample_rate: int) -> bytes:
    validated = _validate_samples(samples)
    pcm = bytearray(len(validated) * 2)
    for idx, clamped in enumerate(validated):
        ival = int(round(clamped * 32767.0))
        struct.pack_into("<h", pcm, idx * 2, ival)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(bytes(pcm))
    return buf.getvalue()


def _default_ming3_loader(model_dir: Path, manifest: dict[str, Any]) -> Any:
    try:
        import ming_omni_tts  # type: ignore[import-not-found]
    except ImportError as exc:
        raise TTSBackendError(
            "Ming 3 Tiny runtime is not installed in the configured GPU voice environment"
        ) from exc
    runtime_rev = getattr(ming_omni_tts, "__git_revision__", None) or getattr(
        ming_omni_tts, "RUNTIME_REVISION", None
    )
    if runtime_rev != manifest["runtime_revision"]:
        raise TTSBackendError(
            "Ming 3 Tiny runtime revision does not match pinned manifest runtime_revision"
        )
    load_fn = getattr(ming_omni_tts, "load_ming3_tiny", None)
    if not callable(load_fn):
        raise TTSBackendError("Ming 3 Tiny runtime does not expose load_ming3_tiny")
    return load_fn(
        model_dir=str(model_dir),
        device="cuda:0",
        local_files_only=True,
        trust_remote_code=False,
    )


class Ming3TinyBackend:
    """Buffered CUDA-only TTS backend for inclusionAI/Ming-3.0-tts-tiny."""

    sample_rate = MING3_SAMPLE_RATE
    artifact_revision = MING3_REVISION
    runtime_revision = MING3_RUNTIME_REVISION

    def __init__(
        self,
        model_dir: str | Path | None,
        voice: str = MING3_VOICE,
        *,
        device: str = "cuda",
        manifest: dict[str, Any] = MANIFEST,
        loader: Any = None,
    ) -> None:
        if device != "cuda":
            raise ValueError("Ming 3 Tiny requires device='cuda'")
        if voice not in MING3_VOICES:
            raise ValueError(f"Ming 3 Tiny requires configured voice {MING3_VOICE}")
        self.device = device
        self.voice = voice
        self.manifest = manifest
        self._raw_model_dir = model_dir
        self.model_dir: Path | None = None
        self._loader = loader or _default_ming3_loader
        self._runtime: Any = None
        self.assets_state = "not_checked"
        self.loaded_device: str | None = None
        self._closed = False
        if loader is not None:
            self.load()

    def load(self) -> None:
        if self._runtime is not None:
            return
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        self.model_dir = verify_ming3_assets(self._raw_model_dir, self.manifest)
        self._runtime = self._loader(self.model_dir, self.manifest)
        raw_model = getattr(self._runtime, "model", self._runtime)
        self.loaded_device = _verify_cuda_residency(raw_model)
        self.assets_state = "verified"

    def _raw_model(self) -> Any:
        return getattr(self._runtime, "model", self._runtime)

    def _generate_validated_samples(
        self,
        text: str,
        *,
        voice: str | None = None,
        cancel: Any = None,
        task_type: str = MING3_TASK_TYPE,
        emotion: str | None = None,
        voice_description: str | None = None,
    ) -> list[float]:
        if self._closed:
            raise TTSBackendError("Ming 3 Tiny backend is closed")
        if self._runtime is None:
            self.load()
        selected_voice = self.voice if voice is None else voice
        cleaned, validated_voice = validate_ming3_request(
            text,
            voice=selected_voice,
            task_type=task_type,
            emotion=emotion,
            voice_description=voice_description,
        )
        if cancel is not None and cancel.is_set():
            raise SynthesisCancelled("Ming 3 Tiny synthesis cancelled before inference")
        result = self._runtime.generate(
            text=cleaned,
            voice=validated_voice,
            task_type=task_type,
            trust_remote_code=False,
            local_files_only=True,
            cancel=cancel,
        )
        if cancel is not None and cancel.is_set():
            raise SynthesisCancelled("Ming 3 Tiny synthesis cancelled during inference")
        if isinstance(result, dict):
            if result.get("truncated") or result.get("stopped_by_max_tokens"):
                raise TTSBackendError("Ming 3 Tiny synthesis truncated before natural completion")
            samples = result.get("samples")
            rate = int(result.get("sample_rate", self.sample_rate))
        else:
            samples = result
            rate = self.sample_rate
        if rate != self.sample_rate:
            raise TTSBackendError(
                f"Ming 3 Tiny sample rate mismatch: expected {self.sample_rate}, got {rate}"
            )
        if not isinstance(samples, (list, tuple)):
            raise TTSBackendError("Ming 3 Tiny runtime must return a sequence of float samples")
        return _validate_samples(samples)

    def produce(self, text: str, emit: Any, cancelled: Any) -> None:
        samples = self._generate_validated_samples(
            text,
            voice=self.voice,
            cancel=cancelled,
        )
        if cancelled is not None and cancelled.is_set():
            raise SynthesisCancelled("Ming 3 Tiny synthesis cancelled before emission")
        emit(AudioWaveform(samples, self.sample_rate))

    def synthesize(
        self,
        text: str,
        voice: str | None = None,
        cancel: Any = None,
        *,
        task_type: str = MING3_TASK_TYPE,
        emotion: str | None = None,
        voice_description: str | None = None,
    ) -> bytes:
        samples = self._generate_validated_samples(
            text,
            voice=voice,
            cancel=cancel,
            task_type=task_type,
            emotion=emotion,
            voice_description=voice_description,
        )
        return _pcm16_wav_from_samples(samples, self.sample_rate)

    def stream_chunks(
        self,
        text: str,
        voice: str | None = None,
        cancel: Any = None,
    ):
        wav_bytes = self.synthesize(text, voice=voice, cancel=cancel)
        with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
            pcm = reader.readframes(reader.getnframes())
        if cancel is not None and cancel.is_set():
            raise SynthesisCancelled("Ming 3 Tiny synthesis cancelled before chunk emission")
        yield pcm

    def close(self) -> None:
        self._closed = True
        close_fn = getattr(self._runtime, "close", None)
        if callable(close_fn):
            close_fn()
