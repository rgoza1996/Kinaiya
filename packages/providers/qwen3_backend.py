"""Bounded native Qwen3 CPU dialogue engine; importing loads no model runtime.

Qwen3Engine lazily imports the optional Torch/Transformers runtime, pins the
CPU-only float32/eager safetensors configuration with local-only files and no
remote code, and streams actual decoded Unicode text through a bounded queue.
Model output is untrusted text: it is never a tool call or permission grant.
Thread failures surface as fixed codes, never raw errors or prompt text.
Closing a generator never proves the native thread stopped; the worker owns
process termination when a producer remains live or uncertain.
"""

import queue
import threading
from pathlib import Path

from packages.providers import qwen3_ipc


class Qwen3InputLimitError(ValueError):
    """Rendered prompt exceeded the configured input token bound."""


class Qwen3OutputLimitError(ValueError):
    """Decoded output exceeded its finite text/event bounds."""


class Qwen3GenerationError(RuntimeError):
    """Native generation failed or was misused; worker maps to a fixed code."""


def _split_utf8(text, limit):
    """Split text into non-empty chunks of at most limit UTF-8 bytes."""
    if not isinstance(text, str) or not text:
        raise ValueError("Qwen3 deltas require non-empty text")
    chunk, size = [], 0
    for char in text:
        encoded = len(char.encode("utf-8"))
        if size + encoded > limit:
            yield "".join(chunk)
            chunk, size = [], 0
        chunk.append(char)
        size += encoded
    yield "".join(chunk)


class Qwen3Engine:
    """Resident Qwen3 CPU engine bound to one sealed snapshot directory."""

    def __init__(self, snapshot_dir, max_input_tokens=2048, max_new_tokens=128):
        limits = qwen3_ipc.validate_limits(max_input_tokens, max_new_tokens)
        snapshot_dir = Path(snapshot_dir)
        pinned = set(qwen3_ipc.MANIFEST["assets"])
        if (
            not snapshot_dir.is_absolute()
            or {entry.name for entry in snapshot_dir.iterdir()} != pinned
        ):
            raise ValueError("Qwen3 engine requires the exact sealed snapshot")
        self._snapshot = snapshot_dir
        self._max_input_tokens = limits["max_input_tokens"]
        self._max_new_tokens = limits["max_new_tokens"]
        # Optional native runtime imports stay lazy so module import and the
        # worker's network/file fences never depend on Torch/Transformers.
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)
        self._torch = torch
        self._tokenizer = AutoTokenizer.from_pretrained(
            str(snapshot_dir),
            local_files_only=True,
            trust_remote_code=False,
            use_fast=True,
        )
        self._model = AutoModelForCausalLM.from_pretrained(
            str(snapshot_dir),
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
            dtype=torch.float32,
            device_map="cpu",
            attn_implementation="eager",
        )
        self._model.eval()
        eos = self._model.generation_config.eos_token_id
        self._eos = {eos} if type(eos) is int else set(eos)
        if not self._eos or any(type(value) is not int or value < 0 for value in self._eos):
            raise ValueError("Qwen3 requires explicit EOS token identities")
        self._lock = threading.Lock()
        self._producer = None
        self._orphaned = False

    def producer_alive(self):
        producer = self._producer
        return producer is not None and producer.is_alive()

    def generate(self, messages):
        """Yield decoded delta dicts then one done dict after producer join."""
        messages = qwen3_ipc.validate_messages(messages)
        if self._orphaned or not self._lock.acquire(blocking=False):
            raise Qwen3GenerationError("Qwen3 refuses overlapping generation")
        try:
            rendered = self._tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            inputs = self._tokenizer(rendered, return_tensors="pt", add_special_tokens=False)
            input_ids = inputs["input_ids"]
            input_tokens = int(input_ids.shape[-1])
            if input_tokens < 1:
                raise Qwen3GenerationError("Qwen3 rendered no input tokens")
            if input_tokens > self._max_input_tokens:
                # Refuse oversized prompts; never truncate personality/history.
                raise Qwen3InputLimitError("Qwen3 input exceeds its token bound")
            from transformers import TextStreamer

            items = queue.Queue(maxsize=8)
            stopped = threading.Event()

            def put(item):
                while not stopped.is_set():
                    try:
                        items.put(item, timeout=0.05)
                        return True
                    except queue.Full:
                        continue
                return False

            class _QueueStreamer(TextStreamer):
                """Decode finalized text into the bounded queue.

                Callback timing is not usable text proof; only the finalized
                decoded string carried here counts as generated text.
                """

                def __init__(self, tokenizer):
                    super().__init__(tokenizer, skip_prompt=True, skip_special_tokens=True)
                    self._bytes = 0

                def on_finalized_text(self, text, stream_end=False):
                    if text:
                        self._bytes += len(text.encode("utf-8"))
                        if self._bytes > qwen3_ipc.MAX_TEXT_BYTES:
                            raise Qwen3OutputLimitError("Qwen3 output exceeds its text bound")
                        for chunk in _split_utf8(text, qwen3_ipc.MAX_DELTA_BYTES):
                            if not put(chunk):
                                raise Qwen3GenerationError("Qwen3 consumer closed")

            streamer = _QueueStreamer(self._tokenizer)
            box, failure = {}, {}

            def _produce():
                try:
                    # Inference mode is thread-local: it must enclose the
                    # actual producer, not just the iterator's calling thread.
                    with self._torch.inference_mode():
                        box["output"] = self._model.generate(
                            **inputs,
                            do_sample=False,
                            num_beams=1,
                            max_new_tokens=self._max_new_tokens,
                            use_cache=True,
                            streamer=streamer,
                        )
                except Exception as exc:  # Fixed code only; never raw text.
                    failure["exc"] = exc
                finally:
                    put(None)

            producer = threading.Thread(target=_produce, daemon=True, name="qwen3-generate")
            self._producer = producer
            producer.start()
            try:
                event_count, decoded_total = 0, 0
                while True:
                    piece = items.get()
                    if piece is None:
                        break
                    if not isinstance(piece, str) or not piece:
                        continue
                    decoded_total += len(piece.encode("utf-8"))
                    if decoded_total > qwen3_ipc.MAX_TEXT_BYTES:
                        raise Qwen3OutputLimitError("Qwen3 output exceeds its text bound")
                    for chunk in _split_utf8(piece, qwen3_ipc.MAX_DELTA_BYTES):
                        event_count += 1
                        if event_count >= qwen3_ipc.MAX_EVENTS:
                            raise Qwen3OutputLimitError("Qwen3 output exceeds its event bound")
                        yield {"kind": "delta", "text": chunk}
                producer.join()
                if isinstance(failure.get("exc"), Qwen3OutputLimitError):
                    raise Qwen3OutputLimitError("Qwen3 output exceeds its text bound")
                if failure.get("exc") is not None or "output" not in box:
                    raise Qwen3GenerationError("Qwen3 native generation failed")
                output_tokens = int(box["output"].shape[1] - input_tokens)
                if output_tokens < 1:
                    raise Qwen3GenerationError("Qwen3 produced no output tokens")
                if output_tokens > self._max_new_tokens:
                    raise Qwen3OutputLimitError("Qwen3 output exceeds its token bound")
                event_count += 1
                if event_count > qwen3_ipc.MAX_EVENTS:
                    raise Qwen3OutputLimitError("Qwen3 output exceeds its event bound")
                last_token = int(box["output"][0, -1])
                if last_token in self._eos:
                    finish = "eos"
                elif output_tokens == self._max_new_tokens:
                    finish = "length"
                else:
                    raise Qwen3GenerationError("Qwen3 stopped without EOS or its token limit")
                yield {
                    "kind": "done",
                    "finish_reason": finish,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }
            finally:
                stopped.set()
                producer.join(timeout=0.1)
                if producer.is_alive():
                    # Generator close is not proof the native thread stopped;
                    # the worker must terminate this dedicated child.
                    self._orphaned = True
        finally:
            if not self.producer_alive():
                self._producer = None
            self._lock.release()
