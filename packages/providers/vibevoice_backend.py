"""Pinned, offline VibeVoice CPU synthesis on the caller-owned producer thread.

Imports of optional inference packages occur only after asset/runtime validation.
The worker owns concurrency, queue backpressure, cancellation and error reporting;
this adapter never creates a thread or turns a generation error into normal EOF.
"""

import copy
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

from packages.providers.tts_backends import AudioWaveform, TTSBackendError

VIBEVOICE_REVISION = "6bce5f06044837fe6d2c5d7a71a84f0416bd57e4"
VIBEVOICE_VOICE = "en-Carter_man"
RUNTIME_REVISION = "16fb2cb1217c9934a886e1948ffb06120caa2df5"
TOKENIZER_REVISION = "060db6499f32faf8b98477b0a26969ef7d8b9987"
SAMPLE_RATE = 24000
_MANIFEST = Path(__file__).with_name("vibevoice_manifest.json")
_REQUIRED = frozenset(
    {
        "checkpoint/config.json",
        "checkpoint/preprocessor_config.json",
        "checkpoint/model.safetensors",
        "checkpoint/README.md",
        "tokenizer/tokenizer.json",
        "tokenizer/tokenizer_config.json",
        "tokenizer/vocab.json",
        "tokenizer/merges.txt",
        "tokenizer/config.json",
        "tokenizer/LICENSE",
        "voices/en-Carter_man.pt",
        "licenses/runtime-MIT.txt",
    }
)


def _manifest():
    value = json.loads(_MANIFEST.read_text())
    if (
        value.get("revision") != VIBEVOICE_REVISION
        or value.get("runtime_revision") != RUNTIME_REVISION
        or value.get("tokenizer_revision") != TOKENIZER_REVISION
        or value.get("voice") != VIBEVOICE_VOICE
        or set(value.get("files", {})) != _REQUIRED
        or value.get("runtime_versions")
        != {"vibevoice": "1.0.0", "transformers": "4.51.3", "torch": "2.9.1+cpu"}
    ):
        raise TTSBackendError("Invalid pinned voice manifest")
    return value


def _verify_files(root, records):
    """Open every component without following symlinks, then hash regular files.

    The owner must protect assets/runtime against mutation after verification;
    later library opens are not an immutable snapshot of these descriptors.
    """
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for relative, record in sorted(records.items()):
            parts = relative.split("/")
            if any(part in {"", ".", ".."} for part in parts):
                raise TTSBackendError("Invalid pinned voice manifest")
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


def _verify_runtime(manifest):
    for package, version in manifest["runtime_versions"].items():
        if importlib.metadata.version(package) != version:
            raise TTSBackendError("The pinned CPU voice runtime is required")
    distribution = importlib.metadata.distribution("vibevoice")
    files = manifest.get("runtime_files", {})
    actual = {
        str(path)
        for path in distribution.files or []
        if str(path).startswith("vibevoice/") and str(path).endswith(".py")
    }
    if not files or set(files) != actual:
        raise TTSBackendError("The pinned voice implementation is required")
    runtime_root = Path(distribution.locate_file(""))
    spec = importlib.util.find_spec("vibevoice")
    if spec is None or spec.origin != str(runtime_root / "vibevoice/__init__.py"):
        raise TTSBackendError("The pinned voice implementation is required")
    _verify_files(runtime_root, files)


def _runtime_modules():
    import torch
    from transformers.cache_utils import DynamicCache
    from transformers.modeling_outputs import BaseModelOutputWithPast
    from vibevoice.modular.modeling_vibevoice_streaming_inference import (
        VibeVoiceStreamingForConditionalGenerationInference,
    )
    from vibevoice.modular.modular_vibevoice_text_tokenizer import VibeVoiceTextTokenizerFast
    from vibevoice.processor.vibevoice_streaming_processor import VibeVoiceStreamingProcessor
    from vibevoice.processor.vibevoice_tokenizer_processor import VibeVoiceTokenizerProcessor

    return SimpleNamespace(
        torch=torch,
        Model=VibeVoiceStreamingForConditionalGenerationInference,
        Tokenizer=VibeVoiceTextTokenizerFast,
        Processor=VibeVoiceStreamingProcessor,
        AudioProcessor=VibeVoiceTokenizerProcessor,
        DynamicCache=DynamicCache,
        ModelOutput=BaseModelOutputWithPast,
    )


class _CallbackStreamer:
    """Native streamer's two-method interface, with no internal queue/thread."""

    def __init__(self, emit, cancelled, torch):
        self.emit, self.cancelled, self.torch = emit, cancelled, torch
        self.finished = False
        self.samples = 0

    def put(self, chunks, sample_indices):
        if self.cancelled.is_set():
            raise TTSBackendError("Voice request cancelled")
        # Upstream may finish sample zero before exiting its acoustic window.
        if self.finished:
            return
        if len(sample_indices) != 1 or sample_indices[0].item() != 0 or len(chunks) != 1:
            raise TTSBackendError("Voice waveform must have one sample")
        values = chunks[0].detach().cpu().to(self.torch.float32).numpy()
        if values.ndim == 2 and values.shape[0] == 1:
            values = values[0]
        if values.ndim != 1 or len(values) < 1:
            raise TTSBackendError("Voice waveform must be mono")
        self.samples += len(values)
        if self.samples > SAMPLE_RATE * 120:
            raise TTSBackendError("Voice audio exceeds bounded duration")
        # emit validates PCM and may block for worker-owned backpressure or
        # raise cancellation. Let that error unwind native generation directly.
        self.emit(AudioWaveform(values, SAMPLE_RATE))

    def end(self, sample_indices=None):
        self.finished = True


class VibeVoiceBackend:
    def __init__(self, model_dir, voice):
        if voice != VIBEVOICE_VOICE:
            raise TTSBackendError("Unsupported configured TTS voice")
        self.model_dir = Path(model_dir) if model_dir is not None else None
        self.voice = voice
        self.assets_state = "not_checked"
        self.model = self.processor = self.prompt = self.runtime = None

    def load(self):
        if self.model is not None:
            return
        try:
            if self.model_dir is None or not self.model_dir.is_absolute():
                raise TTSBackendError("Pinned local voice assets are required")
            manifest = _manifest()
            _verify_files(self.model_dir, manifest["files"])
            _verify_runtime(manifest)
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            runtime = _runtime_modules()
            torch = runtime.torch
            torch.set_num_threads(4)
            if torch.get_num_interop_threads() != 1:
                torch.set_num_interop_threads(1)
            tokenizer = runtime.Tokenizer.from_pretrained(
                str(self.model_dir / "tokenizer"), local_files_only=True, trust_remote_code=False
            )
            audio_processor = runtime.AudioProcessor(
                sampling_rate=SAMPLE_RATE, normalize_audio=True, target_dB_FS=-25, eps=1e-6
            )
            processor = runtime.Processor(
                tokenizer=tokenizer,
                audio_processor=audio_processor,
                speech_tok_compress_ratio=3200,
                db_normalize=True,
            )
            model = runtime.Model.from_pretrained(
                str(self.model_dir / "checkpoint"),
                local_files_only=True,
                trust_remote_code=False,
                use_safetensors=True,
                torch_dtype=torch.float32,
                device_map="cpu",
                attn_implementation="sdpa",
            )
            model.eval()
            if any(p.device.type != "cpu" or p.dtype != torch.float32 for p in model.parameters()):
                raise TTSBackendError("The CPU voice runtime is required")
            model.model.noise_scheduler = model.model.noise_scheduler.from_config(
                model.model.noise_scheduler.config,
                algorithm_type="sde-dpmsolver++",
                beta_schedule="squaredcos_cap_v2",
            )
            model.set_ddpm_inference_steps(num_steps=5)
            with torch.serialization.safe_globals([runtime.ModelOutput, runtime.DynamicCache]):
                prompt = torch.load(
                    self.model_dir / "voices/en-Carter_man.pt",
                    map_location="cpu",
                    weights_only=True,
                )
            self.model, self.processor, self.prompt, self.runtime = (
                model,
                processor,
                prompt,
                runtime,
            )
            self.assets_state = "verified"
        except Exception:
            raise TTSBackendError(
                "Pinned VibeVoice CPU assets or runtime are unavailable"
            ) from None

    def produce(self, text, emit, cancelled):
        if not isinstance(text, str) or not text.strip() or len(text) > 1200:
            raise TTSBackendError("Voice input exceeds configured bounds")
        if self.model is None:
            raise TTSBackendError("Voice backend has not been loaded")
        if cancelled.is_set():
            raise TTSBackendError("Voice request cancelled")
        prompt = copy.deepcopy(self.prompt)
        inputs = self.processor.process_input_with_cached_prompt(
            text=text.strip().replace("’", "'"),
            cached_prompt=prompt,
            padding=True,
            return_tensors="pt",
            return_attention_mask=True,
        )
        inputs = {
            key: value.to("cpu") if hasattr(value, "to") else value for key, value in inputs.items()
        }
        text_tokens = inputs["tts_text_ids"].shape[-1]
        prefix_tokens = max(inputs["input_ids"].shape[-1], inputs["tts_lm_input_ids"].shape[-1])
        if text_tokens + prefix_tokens >= 8192:
            raise TTSBackendError("Voice input exceeds bounded model context")
        streamer = _CallbackStreamer(emit, cancelled, self.runtime.torch)
        result = self.model.generate(
            **inputs,
            # 7.5 acoustic steps/sec, plus the text tokens interleaved by the
            # native generator. The callback independently caps actual samples.
            max_new_tokens=min(text_tokens + 900, 8192 - prefix_tokens),
            cfg_scale=1.5,
            tokenizer=self.processor.tokenizer,
            generation_config={"do_sample": False},
            audio_streamer=streamer,
            stop_check_fn=cancelled.is_set,
            verbose=False,
            show_progress_bar=False,
            return_speech=False,
            all_prefilled_outputs=prompt,
        )
        if cancelled.is_set():
            raise TTSBackendError("Voice request cancelled")
        reached_limit = getattr(result, "reach_max_step_sample", None)
        if reached_limit is not None and reached_limit.any():
            raise TTSBackendError("Voice generation exceeded bounded model context")
