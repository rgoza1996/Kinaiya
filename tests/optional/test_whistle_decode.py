"""Actual optional PyAV decode/resampling only; no Whistle model/native load.

Run separately with an existing pinned decoder environment:
python -m unittest discover -s tests/optional -p test_whistle_decode.py -v
"""

import importlib.util
import io
import math
import struct
import unittest
import wave

from packages.providers.whistle_audio import decode_wav


def wav(rate, channels, values):
    target = io.BytesIO()
    with wave.open(target, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(struct.pack("<" + "h" * len(values), *values))
    return target.getvalue()


@unittest.skipUnless(importlib.util.find_spec("av"), "optional pinned PyAV is not installed")
class WhistleDecode(unittest.TestCase):
    def test_mono_pcm16_scale_and_sample_count(self):
        result = decode_wav(wav(16000, 1, [4096] * 1600))
        self.assertEqual(len(result), 1600)
        self.assertTrue(all(abs(value - 0.125) < 1e-6 for value in result))

    def test_stereo_downmix_cancels_opposite_channels(self):
        result = decode_wav(wav(48000, 2, [8192, -8192] * 4800))
        self.assertEqual(len(result), 1600)
        self.assertLess(max(map(abs, result)), 1e-6)

    def test_same_channel_stereo_uses_unity_sum_downmix_gain(self):
        half = decode_wav(wav(16000, 2, [16384, 16384] * 1600))
        self.assertTrue(all(abs(value - 0.5) < 1e-6 for value in half))
        for value in (-32768, 32767):
            result = decode_wav(wav(16000, 2, [value, value] * 1600))
            self.assertGreaterEqual(min(result), -1.0)
            self.assertLessEqual(max(result), 1.0)

    def test_resampler_step_overshoot_is_limited_to_native_pcm_contract(self):
        for rate in (8000, 48000):
            with self.subTest(rate=rate):
                values = [-32768] * (rate // 20) + [32767] * (rate // 20)
                result = decode_wav(wav(rate, 1, values))
                self.assertEqual(len(result), 1600)
                self.assertTrue(all(math.isfinite(value) and -1 <= value <= 1 for value in result))

    def test_resampling_preserves_tone_and_rejects_above_nyquist(self):
        def rms(frequency):
            values = [
                round(8192 * math.sin(2 * math.pi * frequency * i / 48000)) for i in range(4800)
            ]
            result = decode_wav(wav(48000, 1, values))
            self.assertEqual(len(result), 1600)
            return math.sqrt(
                sum(value * value for value in result[100:-100]) / len(result[100:-100])
            )

        self.assertGreater(rms(440), 0.15)
        self.assertLess(rms(12000), 0.005)

    def test_44100_to_16000_retains_duration(self):
        result = decode_wav(wav(44100, 1, [0] * 4410))
        self.assertEqual(len(result), 1600)
