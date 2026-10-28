"""Deterministic boundaries; no Whistle library/model/inference is used."""

import errno
import hashlib
import io
import json
import os
import struct
import wave

import pytest

from packages.providers.whistle_assets import MANIFEST, sealed_asset
from packages.providers.whistle_audio import MAX_AUDIO_BYTES, inspect_wav
from packages.providers.whistle_backend import MAX_RESPONSE_BYTES, transcript
from packages.providers.whistle_ipc import decode_json, encode_json, read_frame, token, write_frame
from services.resources.whistle import ManagedWhistleConfig


def wav(*, rate=16000, channels=1, frames=160, width=2):
    target = io.BytesIO()
    with wave.open(target, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(bytes(frames * channels * width))
    return target.getvalue()


@pytest.mark.parametrize(
    "rate,channels,width",
    [(8000, 1, 1), (16000, 1, 2), (44100, 2, 3), (48000, 2, 4), (192000, 1, 2)],
)
def test_supported_wav_metadata(rate, channels, width):
    data = wav(rate=rate, channels=channels, width=width)
    assert inspect_wav(data) == {
        "sample_rate": rate,
        "channels": channels,
        "frames": 160,
        "seconds": 160 / rate,
    }


def test_exact_duration_and_body_limits():
    assert inspect_wav(wav(frames=30 * 16000))["seconds"] == 30
    with pytest.raises(ValueError):
        inspect_wav(wav(frames=30 * 16000 + 1))
    with pytest.raises(ValueError):
        inspect_wav(bytes(MAX_AUDIO_BYTES + 1))


@pytest.mark.parametrize(
    "offset,value,fmt",
    [
        (0, b"NOPE", None),
        (4, 99, "<I"),
        (20, 3, "<H"),
        (22, 3, "<H"),
        (24, 7999, "<I"),
        (28, 1, "<I"),
        (32, 1, "<H"),
        (34, 12, "<H"),
        (40, 99999, "<I"),
    ],
)
def test_bad_wav_headers_are_refused(offset, value, fmt):
    data = bytearray(wav())
    if fmt:
        struct.pack_into(fmt, data, offset, value)
    else:
        data[offset : offset + len(value)] = value
    with pytest.raises(ValueError):
        inspect_wav(bytes(data))


@pytest.mark.parametrize("data", [b"", b"RIFF", b"x" * 44, wav()[:-1], wav(frames=0)])
def test_empty_truncated_and_non_wav(data):
    with pytest.raises(ValueError):
        inspect_wav(data)


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b'{"text":1}',
        b'{"text":null}',
        b'{"text":"a","text":"b"}',
        b'{"text":"a","extra":NaN}',
        b'{"text":"\\ud800"}',
        b"x" * MAX_RESPONSE_BYTES,
    ],
)
def test_invalid_native_result(raw):
    with pytest.raises((ValueError, UnicodeError)):
        transcript(raw)


def test_native_result_projects_only_bounded_text():
    assert (
        transcript(
            json.dumps(
                {"text": "Public synthetic π", "words": [{"unused": "not forwarded"}]}
            ).encode()
        )
        == "Public synthetic π"
    )
    assert transcript(json.dumps({"text": "x" * 16000}).encode()) == "x" * 16000
    with pytest.raises(ValueError):
        transcript(json.dumps({"text": "x" * 16001}).encode())


def test_pins_are_fixed_and_offline():
    assert (
        MANIFEST["weights"]["sha256"]
        == "b6e02f048568ac5d01a2042556c658061e699acbc0aa2a1439f52f3d461dffeb"
    )
    assert (
        MANIFEST["library"]["sha256"]
        == "87476ff70e80334d7c766dbf236b72f42876072df22014c6ec8a9b087b82b260"
    )
    with pytest.raises(ValueError):
        ManagedWhistleConfig(
            python="/usr/bin/python",
            weights="/owner/model",
            library="/owner/library",
            expected_sha256="unreviewed",
        )


def test_asset_is_sealed_and_immune_to_later_owner_writes(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    source = tmp_path / "public-fixture.bin"
    raw = b"public synthetic pinned bytes"
    source.write_bytes(raw)
    pin = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    with sealed_asset(source, pin, workspace) as fd:
        source.write_bytes(b"changed")
        assert os.read(fd, 1024) == raw
        with pytest.raises(OSError) as error:
            os.write(fd, b"x")
        assert error.value.errno == errno.EPERM


@pytest.mark.parametrize("bad", ["digest", "size", "workspace", "symlink"])
def test_asset_rejections_before_load(tmp_path, bad):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    source = (workspace if bad == "workspace" else tmp_path) / "fixture.bin"
    source.write_bytes(b"fixture")
    pin = {"bytes": 7, "sha256": hashlib.sha256(b"fixture").hexdigest()}
    if bad == "digest":
        pin["sha256"] = "0" * 64
    if bad == "size":
        pin["bytes"] += 1
    if bad == "symlink":
        link = tmp_path / "linked.bin"
        link.symlink_to(source)
        source = link
    with pytest.raises((ValueError, OSError)):
        with sealed_asset(source, pin, workspace):
            pytest.fail("Unverified artifact reached load boundary")


def test_framing_roundtrip_and_oversized_header():
    read, write = os.pipe()
    try:
        raw = encode_json({"nonce": "a" * 64})
        write_frame(write, raw, 100)
        assert decode_json(read_frame(read, 100)) == {"nonce": "a" * 64}
        os.write(write, struct.pack("!I", 101))
        with pytest.raises(ValueError):
            read_frame(read, 100)
    finally:
        os.close(read)
        os.close(write)


@pytest.mark.parametrize(
    "raw",
    [b"[]", b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b"\xff", b"{" * 4096, b"x" * 4097],
)
def test_malformed_control_messages(raw):
    with pytest.raises(ValueError):
        decode_json(raw)


@pytest.mark.parametrize("value", [None, True, 1, "a" * 63, "A" * 64, "../" + "a" * 61, []])
def test_bad_incarnation_or_nonce(value):
    assert not token(value)
