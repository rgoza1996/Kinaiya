"""Exact identity PCM16 rules; the real child/multitrack oracle stays unchanged."""

import struct

import pytest

pytest.importorskip("fcntl", reason="Native media decoder is a Linux component")

from packages.resources.media_decoder import DecoderError, _float_pcm16


def _floats(values):
    return struct.pack("<" + "f" * len(values), *values)


def _samples(raw):
    return [value for (value,) in struct.iter_unpack("<h", raw)]


def test_identity_float_pcm16_rounds_ties_to_even_and_saturates():
    values = [-2, -1, -1.5 / 32768, -0.5 / 32768, 0, 0.5 / 32768, 1.5 / 32768, 1, 2]
    assert _samples(_float_pcm16([_floats(values)], len(values), 1, False)) == [
        -32768,
        -32768,
        -2,
        0,
        0,
        0,
        2,
        32767,
        32767,
    ]


@pytest.mark.parametrize("planar", [False, True])
def test_identity_stereo_keeps_channel_order_and_ignores_plane_padding(planar):
    left, right = [0.125, 0.25, -1], [-0.25, -0.125, 1]
    planes = (
        [_floats(left) + b"padding", _floats(right) + b"padding"]
        if planar
        else [
            _floats([value for pair in zip(left, right, strict=True) for value in pair])
            + b"padding"
        ]
    )
    assert _samples(_float_pcm16(planes, 3, 2, planar)) == [4096, -8192, 8192, -4096, -32768, 32767]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_decoded_samples_refuse_instead_of_minting_pcm(value):
    with pytest.raises(DecoderError):
        _float_pcm16([_floats([value])], 1, 1, False)


@pytest.mark.parametrize(
    "planes,samples,channels,planar",
    [
        ([], 1, 1, False),
        ([b""], 1, 1, False),
        ([b"1234"], True, 1, False),
        ([b"1234"], 1, True, False),
        ([b"1234"], 1, 1, 1),
        ([b"1234"], 1, 2, True),
        ([b"1234"], 0, 1, False),
        ([b"1234"], 16_777_216, 2, False),
    ],
)
def test_invalid_or_oversized_plane_geometry_refuses_before_allocation(
    planes, samples, channels, planar
):
    with pytest.raises(DecoderError):
        _float_pcm16(planes, samples, channels, planar)
