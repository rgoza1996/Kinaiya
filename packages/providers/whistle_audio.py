"""Bounded browser PCM WAV validation and optional established FFmpeg resampling."""

import array
import io
import math
import struct

MAX_AUDIO_BYTES = 10_000_000
MAX_AUDIO_SECONDS = 30
SAMPLE_RATE = 16000
MAX_SAMPLES = SAMPLE_RATE * MAX_AUDIO_SECONDS


def inspect_wav(data):
    """Support integer PCM WAV, mono/stereo, 8..192kHz, 8/16/24/32 bits.

    Browser capture currently produces PCM16. Compressed/extensible/float WAV
    is explicitly unsupported in this first profile; no guessed conversion.
    """
    if not isinstance(data, bytes) or not 44 <= len(data) <= MAX_AUDIO_BYTES:
        raise ValueError("Audio must be a bounded PCM WAV body")
    if (
        data[:4] != b"RIFF"
        or data[8:12] != b"WAVE"
        or struct.unpack_from("<I", data, 4)[0] != len(data) - 8
    ):
        raise ValueError("Invalid WAV container")
    at, fmt, audio, chunks = 12, None, None, 0
    while at < len(data):
        if at + 8 > len(data) or chunks >= 128:
            raise ValueError("Invalid WAV chunk structure")
        name, size = data[at : at + 4], struct.unpack_from("<I", data, at + 4)[0]
        end = at + 8 + size
        if end + (size & 1) > len(data):
            raise ValueError("Truncated WAV chunk")
        if name == b"fmt ":
            if (
                fmt is not None
                or size not in (16, 18)
                or (size == 18 and data[end - 2 : end] != b"\0\0")
            ):
                raise ValueError("Unsupported WAV format")
            fmt = struct.unpack_from("<HHIIHH", data, at + 8)
        elif name == b"data":
            if audio is not None:
                raise ValueError("Duplicate WAV audio")
            audio = size
        at, chunks = end + (size & 1), chunks + 1
    if fmt is None or audio is None:
        raise ValueError("Missing WAV audio format")
    tag, channels, rate, byte_rate, align, bits = fmt
    if (
        tag != 1
        or channels not in (1, 2)
        or not 8000 <= rate <= 192000
        or bits not in (8, 16, 24, 32)
        or align != channels * (bits // 8)
        or byte_rate != rate * align
        or audio == 0
        or audio % align
    ):
        raise ValueError("Unsupported PCM WAV")
    frames = audio // align
    if frames > rate * MAX_AUDIO_SECONDS:
        raise ValueError("Whistle supports slices of at most 30 seconds")
    return {"sample_rate": rate, "channels": channels, "frames": frames, "seconds": frames / rate}


def decode_wav(data):
    metadata = inspect_wav(data)
    import av

    if av.__version__ != "17.1.0":
        raise RuntimeError("Whistle decoding requires the pinned PyAV 17.1.0 profile")
    samples = array.array("f")
    resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
    # The pinned FFmpeg stereo remix uses equal-power coefficients sqrt(1/2).
    # Normalize its stereo output by sqrt(1/2) to obtain (L + R)/2. Mono input
    # keeps unity gain. Established FFmpeg filtering/resampling stays intact.
    gain = math.sqrt(0.5) if metadata["channels"] == 2 else 1.0

    def append(frames):
        for frame in frames:
            if (
                frame.format.name != "flt"
                or frame.layout.name != "mono"
                or frame.sample_rate != SAMPLE_RATE
            ):
                raise ValueError("Unexpected decoded PCM format")
            samples.frombytes(bytes(frame.planes[0])[: frame.samples * 4])
            if len(samples) > MAX_SAMPLES:
                raise ValueError("Decoded audio exceeds 30 seconds")

    with av.open(io.BytesIO(data), mode="r", format="wav") as container:
        for frame in container.decode(audio=0):
            append(resampler.resample(frame))
        append(resampler.resample(None))
    if not samples or not all(math.isfinite(value) for value in samples):
        raise ValueError("Decoded audio must contain finite mono samples")
    # Band-limited resampling can overshoot even valid integer input. The pinned
    # native header requires [-1, 1]; saturate only after finite validation.
    for index, value in enumerate(samples):
        samples[index] = max(-1.0, min(1.0, value * gain))
    return samples
