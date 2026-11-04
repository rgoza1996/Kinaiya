"""Independent real FD3/FD4 child oracles; prospective child source unread.

Synthetic WAV, still PNG and timed MP4 bytes pass through the original
MediaSpool/MediaWorkerPool/private bootstrap and actual selected PyAV interpreter.
Native references, profile pins
and dependency callbacks are test metadata, not complete Session authority,
qualified runtime closure, publication settlement or public cleanup receipts.
"""

import asyncio
import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
import struct
import sys
import sysconfig
import wave
import zlib
from fractions import Fraction
from pathlib import Path

import pytest

from packages.contracts import media
from packages.resources.media_processes import MediaWorkerPool
from packages.resources.processes import WorkerSpec
from services.common import media_decoder_wire as wire
from services.session.media_spool import MediaSpool

_WAIT = 10
_RATE = 16000
_SAMPLES = 4000
_ROOT = Path(__file__).resolve().parents[2]


def _frozen_helper(name):
    # Load by path so this file also collects alone, before unit tests add their
    # own directory to pytest's import path. No global search path is changed.
    path = _ROOT / "tests" / "unit" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"child_oracle_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


metadata = _frozen_helper("test_media_decoder_contracts")
process_fixtures = _frozen_helper("test_media_processes")
_ENTRY = """
import sys
import av
if av.__version__ != '17.1.0':
    raise SystemExit('Actual selected PyAV17.1.0 is required')
repository, contract_dependencies = sys.argv[1:]
sys.path.insert(0, contract_dependencies)
sys.path.insert(0, repository)
sys.argv = ['packages.resources.media_decoder']
from packages.resources.media_decoder import main
raise SystemExit(main())
"""
_PICTURE_SIZE = (16, 12)
_COLORS = ((32, 64, 96), (160, 96, 32), (64, 160, 96), (192, 160, 128))
_LOSSY_TOLERANCE = 12
_CODEC = """
import base64, io, json, sys
from fractions import Fraction
import av
if av.__version__ != '17.1.0':
    raise SystemExit('Actual selected PyAV17.1.0 is required')
request = json.load(sys.stdin)
if request['action'] == 'video':
    sink = io.BytesIO()
    with av.open(sink, mode='w', format='mp4') as output:
        stream = output.add_stream('libx264', rate=4)
        stream.width, stream.height = request['size']
        stream.pix_fmt = 'yuv420p'
        stream.time_base = Fraction(1, 4)
        stream.options = {'preset': 'ultrafast', 'crf': '12', 'bf': '0'}
        for index, color in enumerate(request['colors']):
            frame = av.VideoFrame(stream.width, stream.height, 'rgb24')
            plane = frame.planes[0]
            row = bytes(color) * stream.width
            plane.update(b''.join(row + bytes(plane.line_size - len(row))
                                  for _ in range(stream.height)))
            frame.pts = index + 7
            frame.time_base = Fraction(1, 4)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode(None):
            output.mux(packet)
    source = sink.getvalue()
else:
    source = base64.b64decode(request['source'], validate=True)
with av.open(io.BytesIO(source), mode='r') as container:
    stream = container.streams.video[0]
    frames = []
    for original in container.decode(stream):
        rgb = original.reformat(format='rgb24')
        plane = rgb.planes[0]
        padded = bytes(plane)
        pixels = b''.join(padded[row * plane.line_size:row * plane.line_size + rgb.width * 3]
                          for row in range(rgb.height))
        frames.append({'pts': original.pts,
                       'timebase': [original.time_base.numerator, original.time_base.denominator]
                           if original.time_base is not None else None,
                       'size': [rgb.width, rgb.height], 'pixels': pixels.hex()})
    report = {'codec': stream.codec_context.name, 'frames': frames,
              'source': base64.b64encode(source).decode()}
json.dump(report, sys.stdout, sort_keys=True)
"""
_WAV_INSPECTOR = """
import base64, io, json, struct, sys
import av
if av.__version__ != '17.1.0':
    raise SystemExit('Actual selected PyAV17.1.0 is required')
request = json.load(sys.stdin)
width = request['width']
source = base64.b64decode(request['source'], validate=True)
with av.open(io.BytesIO(source), mode='r') as container:
    assert len(container.streams.audio) == 1
    stream = container.streams.audio[0]
    frames = []
    for frame in container.decode(stream):
        assert len(frame.layout.channels) == 1 and frame.sample_rate == 16000
        assert frame.pts is not None and frame.time_base is not None
        size, code = (2, 'h') if width == 2 else (4, 'i')
        assert frame.format.name in (('s16', 's16p') if width == 2 else ('s32', 's32p'))
        values = struct.unpack('=%d%s' % (frame.samples, code),
                               bytes(frame.planes[0])[:frame.samples * size])
        if width == 3:
            # FFmpeg represents decoded signed PCM24 in the high 24 bits of
            # its signed 32-bit sample. Recover the original exact PCM bytes.
            assert all(value & 255 == 0 for value in values)
            values = [value >> 8 for value in values]
        pcm = b''.join(value.to_bytes(width, 'little', signed=True) for value in values)
        frames.append({'pts': frame.pts, 'samples': frame.samples, 'pcm': pcm.hex(),
                       'timebase': [frame.time_base.numerator, frame.time_base.denominator]})
    report = {'codec': stream.codec_context.name, 'frames': frames}
json.dump(report, sys.stdout, sort_keys=True)
"""
_GAPPED_AUDIO_CODEC = """
import base64, io, json, math, struct, sys
from fractions import Fraction
import av
if av.__version__ != '17.1.0':
    raise SystemExit('Actual selected PyAV17.1.0 is required')
sink = io.BytesIO()
with av.open(sink, mode='w', format='mp4') as output:
    stream = output.add_stream('aac', rate=16000)
    stream.layout = 'mono'
    stream.bit_rate = 64000
    stream.time_base = Fraction(1, 16000)

    def mux(packet):
        assert packet.pts is not None and packet.dts is not None
        assert packet.time_base == Fraction(1, 16000)
        if packet.pts >= 2048:
            packet.pts += 2048
            packet.dts += 2048
        output.mux(packet)

    for index in range(4):
        frame = av.AudioFrame(format='fltp', layout='mono', samples=1024)
        frame.sample_rate = 16000
        frame.pts = index * 1024
        frame.time_base = Fraction(1, 16000)
        values = [0.36 * math.sin(2 * math.pi * (330 + index * 110) * sample / 16000)
                  for sample in range(1024)]
        pcm = struct.pack('<1024f', *values)
        plane = frame.planes[0]
        plane.update(pcm + bytes(plane.buffer_size - len(pcm)))
        for packet in stream.encode(frame):
            mux(packet)
    for packet in stream.encode(None):
        mux(packet)
source = sink.getvalue()
with av.open(io.BytesIO(source), mode='r') as container:
    stream = container.streams.audio[0]
    frames = []
    for frame in container.decode(stream):
        assert frame.format.name in ('fltp', 'flt') and frame.layout.name == 'mono'
        assert frame.sample_rate == 16000 and frame.pts is not None
        assert frame.time_base is not None
        # Independent scalar sample conversion, without candidate resampling,
        # chunking, gap handling or decode helpers. AAC source samples are lossy;
        # the actual decoded float sample values are the golden source.
        values = struct.unpack('=%df' % frame.samples,
                               bytes(frame.planes[0])[:frame.samples * 4])
        assert all(math.isfinite(value) for value in values)
        pcm = b''.join(struct.pack('<h', max(-32768, min(32767, round(value * 32768))))
                       for value in values)
        frames.append({'pts': frame.pts, 'samples': frame.samples, 'pcm': pcm.hex(),
                       'timebase': [frame.time_base.numerator, frame.time_base.denominator]})
    report = {'codec': stream.codec_context.name, 'frames': frames,
              'source': base64.b64encode(source).decode()}
json.dump(report, sys.stdout, sort_keys=True)
"""


def _json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _frame(header):
    raw = header if type(header) is bytes else _json(header)
    return struct.pack(">I", len(raw)) + raw


def _ref(index):
    return {
        "backend_id": "a" * 32,
        "receipt_id": f"{index:032x}",
        "sha256": hashlib.sha256(f"synthetic-ref-{index}".encode()).hexdigest(),
    }


def _wave_source(width):
    limit = (1 << (8 * width - 1)) - 1
    values = (0, 1, -1, limit // 3, -(limit // 4), 12345, -23456)
    pcm = b"".join(
        values[index % len(values)].to_bytes(width, "little", signed=True)
        for index in range(_SAMPLES)
    )
    sink = io.BytesIO()
    with wave.open(sink, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(width)
        stream.setframerate(_RATE)
        stream.writeframes(pcm)
    return sink.getvalue(), pcm


def _png_source():
    width, height = _PICTURE_SIZE
    row = bytes(_COLORS[0]) * width

    def chunk(kind, body):
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + row) * height))
        + chunk(b"IEND", b"")
    )


def _interpreter():
    selected = os.environ.get("KINAIYA_TEST_MEDIA_DECODER_PYTHON")
    if selected is None:
        if importlib.util.find_spec("av") is None:
            pytest.skip(
                "Actual PyAV17.1.0 child unavailable; select KINAIYA_TEST_MEDIA_DECODER_PYTHON. "
                "This skip is not decoder qualification."
            )
        return sys.executable
    path = Path(selected)
    assert path.is_absolute() and path.is_file() and os.access(path, os.X_OK), (
        "Explicit child interpreter must be an existing executable absolute path"
    )
    return str(path)


async def _actual_codec(harness, request, *, program=_CODEC):
    # This independently waited inspector/encoder does not occupy a native
    # MediaWorkerPool slot or execute any candidate decoder methods. Capture it
    # in the same teardown registry before consuming its result.
    process = await asyncio.wait_for(
        harness.spawn(
            _interpreter(),
            "-B",
            "-c",
            program,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        ),
        _WAIT,
    )
    harness.codec_processes.append(process)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(_json(request)), _WAIT)
        assert process.returncode == 0, (
            "Required actual PyAV codec fixture/inspection failed: "
            + stderr.decode("utf-8", "replace")[-4096:]
        )
        return json.loads(stdout)
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), _WAIT)


@pytest.fixture
async def native_harness(tmp_path, monkeypatch):
    harness = process_fixtures._Harness(tmp_path, monkeypatch)
    harness.codec_processes = []
    try:
        yield harness
    finally:
        # Existing harness fences retained launches, closes owners and waits
        # every captured original OS Process even when a test assertion fails.
        await harness.close()


def _decoder_processes(harness):
    return [process for process in harness.processes if process not in harness.codec_processes]


def _original_audio_anchor(pts, frames, sample):
    matches = [
        frame
        for frame in frames
        if frame["pts"] == pts.pts
        and frame["timebase"] == [pts.time_base_num, pts.time_base_den]
        and frame["sample_start"] <= sample < frame["sample_start"] + frame["samples"]
    ]
    assert len(matches) == 1, "SourcePTS must be an actual original frame anchor owning this sample"
    return matches[0]


class _Child:
    def __init__(
        self, harness, pool, launch, channel, process, binding, profile, source, pcm, width
    ):
        self.harness = harness
        self.pool = pool
        self.launch = launch
        self.channel = channel
        self.process = process
        self.binding = binding
        self.profile = profile
        self.source = source
        self.pcm = pcm
        self.width = width
        self.seal = _ref(200)
        self.child_owner = _ref(201)
        self.observation = None
        self.audio_frames = []
        self.seq = 0

    def request(self, command, plan=None):
        self.seq += 1
        return {
            "v": 2,
            "operation_id": self.binding.operation_id,
            "source": self.binding.source.model_dump(mode="json"),
            "seal": copy.deepcopy(self.seal),
            "consumer": self.binding.consumer.model_dump(mode="json"),
            "fence": self.binding.fence.model_dump(mode="json"),
            "child_owner": copy.deepcopy(self.child_owner),
            "profile_sha256": self.profile["profile_sha256"],
            "seq": self.seq,
            "command": command,
            "profile": copy.deepcopy(self.profile) if command == "probe" else None,
            "plan": copy.deepcopy(plan),
        }

    async def send(self, request):
        # Revalidate positive fixture requests before crossing the real socket.
        typed = media.media_json(_json(request), media.MediaDecodeRequestV2)
        await asyncio.wait_for(self.channel.send_chunk(wire.decoder_request_v2_bytes(typed)), _WAIT)
        response, body = await self.receive()
        assert response.operation_id == request["operation_id"]
        assert response.seq == request["seq"]
        return response, body

    async def exact(self, size, initial=b""):
        chunks = bytearray(initial)
        while len(chunks) < size:
            part = await asyncio.wait_for(
                self.channel.recv_chunk(min(65536, size - len(chunks))), _WAIT
            )
            assert part, "Original child ended in a partial FD3 frame"
            chunks.extend(part)
        return bytes(chunks)

    async def receive(self, *, allow_eof=False):
        first = await asyncio.wait_for(self.channel.recv_chunk(4), _WAIT)
        if not first:
            assert allow_eof, "Actual child exited before its expected response"
            return None
        prefix = await self.exact(4, first)
        size = struct.unpack(">I", prefix)[0]
        assert 0 < size <= 65536
        header = await self.exact(size)
        raw = json.loads(header)
        packet = raw.get("packet")
        body_size = 0 if packet is None else packet["body_bytes"]
        assert type(body_size) is int and 0 <= body_size <= 16777216
        body = await self.exact(body_size)
        return wire.decoder_response_v2_json(prefix + header + body)

    async def probe_source(self):
        request = self.request("probe")
        response, body = await self.send(request)
        assert response.result == "probe" and body == b""
        observation = response.probe
        assert observation.source == self.binding.source
        assert observation.seal.model_dump(mode="json") == self.seal
        assert observation.child_owner.model_dump(mode="json") == self.child_owner
        assert observation.profile_sha256 == self.profile["profile_sha256"]
        assert observation.probe.source_sha256 == hashlib.sha256(self.source).hexdigest()
        assert observation.probe.input_bytes == len(self.source)
        self.observation = observation.model_dump(mode="json")
        return observation

    async def probe(self):
        observation = await self.probe_source()
        assert observation.probe.container == "wav"
        assert len(observation.probe.tracks) == len(observation.clocks) == 1
        track = observation.probe.tracks[0]
        clock = observation.clocks[0]
        assert track.index == clock.probe_index == 0
        assert track.kind == "audio"
        assert track.codec == f"pcm_s{self.width * 8}le"
        assert track.rate == _RATE and track.channels == 1
        assert clock.first_pts == 0
        assert self.audio_frames and clock.first_pts == self.audio_frames[0]["pts"]
        assert clock.provenance == "container"
        assert (clock.timebase.num, clock.timebase.den) == (1, _RATE)
        assert [clock.timebase.num, clock.timebase.den] == self.audio_frames[0]["timebase"]
        assert clock.timebase == track.timebase

    def open_plan(
        self,
        container,
        *,
        index=0,
        start=Fraction(0),
        end=Fraction(1, 4),
        next_source=17,
        next_track=9,
        format_=None,
    ):
        assert self.observation is not None
        probe_track = self.observation["probe"]["tracks"][0]
        clock = self.observation["clocks"][0]
        if format_ is None:
            format_ = {
                "kind": "audio",
                "container": container,
                "codec": f"pcm_s{self.width * 8}le",
                "rate": _RATE,
                "channels": 1,
            }
        origin = Fraction(clock["first_pts"] * clock["timebase"]["num"], clock["timebase"]["den"])
        track = {
            "track_id": "f" * 32,
            "source_id": self.binding.source_id,
            "kind": probe_track["kind"],
            "format_sha256": metadata._digest("Format", format_),
            "timebase": copy.deepcopy(probe_track["timebase"]),
            "common_origin": {"num": origin.numerator, "den": origin.denominator},
            "clock_provenance": clock["provenance"],
            "command_lane": False,
        }
        segment = {
            "segment_id": f"{70 + index:032x}",
            "index": index,
            "source": copy.deepcopy(self.observation["source"]),
            "source_revision": self.binding.fence.source_revision,
            "generation": self.binding.fence.generation,
            "profile_sha256": self.profile["profile_sha256"],
            "continuity_id": "e" * 32,
            "start": {"num": start.numerator, "den": start.denominator},
            "end": {"num": end.numerator, "den": end.denominator},
            "track_ids": [track["track_id"]],
        }
        return {
            "v": 2,
            "observation": copy.deepcopy(self.observation),
            "ready": {
                "v": 1,
                "source": copy.deepcopy(self.observation["source"]),
                "seal": copy.deepcopy(self.seal),
                "probe": copy.deepcopy(self.observation["probe"]),
                "decoder_profile_sha256": self.profile["profile_sha256"],
                "tracks": [track],
            },
            "selections": [
                {
                    "probe_index": probe_track["index"],
                    "track": copy.deepcopy(track),
                    "format": format_,
                }
            ],
            "segment": segment,
            "budget_ref": _ref(300 + index),
            "budget": {
                "v": 1,
                "segment_id": segment["segment_id"],
                "child_owner": copy.deepcopy(self.child_owner),
                "previous": None if index == 0 else _ref(299 + index),
                "cpu_baseline_ms": 0,
                "wall_baseline_ms": 0,
                "cpu_spent_ms": 0,
                "wall_spent_ms": 0,
                "state": "running",
            },
            "next_source_seq": next_source,
            "cursors": [{"track_id": track["track_id"], "next_seq": next_track}],
        }


async def _start(
    harness,
    width,
    *,
    segments="retained_child",
    binding=None,
    seal=None,
    child_owner=None,
    upload=None,
):
    source, pcm = _wave_source(width)
    inspected = await _actual_codec(
        harness,
        {
            "source": base64.b64encode(source).decode("ascii"),
            "width": width,
        },
        program=_WAV_INSPECTOR,
    )
    assert inspected["codec"] == f"pcm_s{width * 8}le" and inspected["frames"]
    original_frames = inspected["frames"]
    next_sample = 0
    for frame in original_frames:
        assert type(frame["pts"]) is int and frame["timebase"] == [1, _RATE]
        assert type(frame["samples"]) is int and frame["samples"] > 0
        assert frame["pts"] == next_sample
        frame["sample_start"] = next_sample
        next_sample += frame["samples"]
        assert len(bytes.fromhex(frame["pcm"])) == frame["samples"] * width
    assert next_sample == _SAMPLES
    assert b"".join(bytes.fromhex(frame["pcm"]) for frame in original_frames) == pcm
    formats = [
        {
            "kind": "audio",
            "container": container,
            "codec": f"pcm_s{size * 8}le",
            "rate": _RATE,
            "channels": 1,
        }
        for container in ("raw", "wav")
        for size in (2, 3, 4)
    ]
    actual = await _launch(
        harness,
        source,
        formats,
        pcm=pcm,
        width=width,
        segments=segments,
        binding=binding,
        seal=seal,
        child_owner=child_owner,
        upload=upload,
    )
    actual.audio_frames = original_frames
    await actual.probe()
    return actual


async def _launch(
    harness,
    source,
    formats,
    *,
    pcm=None,
    width=None,
    segments="retained_child",
    binding=None,
    seal=None,
    child_owner=None,
    upload=None,
):
    interpreter = _interpreter()
    limits = process_fixtures._limits()
    profile = metadata._profile()
    profile["driver"] = "pyav17_timed_fd_v2"
    profile["protocol"]["kind"] = "decoder_fd_v2"
    profile["protocol"]["segments"] = segments
    profile["limits"] = limits.model_dump(mode="json")
    profile["formats"] = copy.deepcopy(formats)
    profile = metadata._self_hash("DecoderProfile", profile, "profile_sha256")
    # The wrapper loads av from the selected interpreter before adding only the
    # primary contract dependency path. No ambient PYTHONPATH is inherited.
    spec = WorkerSpec(
        id="actual-media-decoder",
        argv=(interpreter, "-B", "-c", _ENTRY, str(_ROOT), sysconfig.get_paths()["purelib"]),
        cwd=_ROOT,
        startup_timeout_s=5,
        stop_timeout_s=0.1,
    )
    pool = MediaWorkerPool(
        spec, limits, current=harness.current, retire_dependencies=harness.retire
    )
    harness.pools.append(pool)
    binding = process_fixtures._binding() if binding is None else binding
    launch = harness.register(pool, binding)
    if upload is None:
        root = harness.root / f"actual-spool-{len(harness.spools)}"
        root.mkdir(mode=0o700)
        spool = MediaSpool(root, limits)
        harness.spools.append(spool)
        upload = spool.reserve(binding.source_id, len(source))
        digest = hashlib.sha256(source).hexdigest()
        upload.append(0, 0, source, digest)
        upload.seal(len(source), digest)
    else:
        assert upload.source_id == binding.source_id
    grant = upload._open_child_grant(launch)
    previous_processes = len(harness.processes)
    child = await asyncio.wait_for(pool.start(launch, grant), _WAIT)
    assert len(harness.processes) == previous_processes + 1
    actual = _Child(
        harness,
        pool,
        launch,
        child.channel,
        harness.processes[previous_processes],
        binding,
        profile,
        source,
        pcm,
        width,
    )
    actual.upload = upload
    if seal is not None:
        actual.seal = copy.deepcopy(seal)
    if child_owner is not None:
        actual.child_owner = copy.deepcopy(child_owner)
    return actual


async def _segment(
    child, plan, container, *, first_sample, first_source, first_track, previous_sequence=0
):
    request = child.request("open", plan)
    packets = []
    actual_pcm = bytearray()
    sample = first_sample
    source_seq, track_seq = first_source, first_track
    packet_sequence = previous_sequence
    for _ in range(100):
        response, body = await child.send(request)
        assert response.segment_id == plan["segment"]["segment_id"]
        if response.result == "end":
            assert body == b"" and response.end.segment_id == plan["segment"]["segment_id"]
            assert response.end.source == child.binding.source
            assert response.end.sealed_source.model_dump(mode="json") == child.seal
            assert response.end.child_owner.model_dump(mode="json") == child.child_owner
            assert response.end.profile_sha256 == child.profile["profile_sha256"]
            assert response.end.through_packet_sequence == packet_sequence
            assert len(response.end.tracks) == 1
            through = response.end.tracks[0]
            assert through.track_id == "f" * 32
            assert through.through_source_seq == source_seq - 1
            assert through.through_track_seq == track_seq - 1
            assert packets and through.last_pts == packets[-1].pts
            return response.end, bytes(actual_pcm), sample, source_seq, track_seq, packet_sequence
        assert response.result == "packet"
        packet = response.packet
        assert packet.operation_id == child.binding.operation_id
        assert packet.source == child.binding.source and packet.consumer == child.binding.consumer
        assert packet.fence == child.binding.fence and packet.track_id == "f" * 32
        assert packet.continuity_id == plan["segment"]["continuity_id"]
        assert packet.payload == "audio" and packet.range is None
        assert packet.source_seq == packet.through_source_seq == source_seq
        assert packet.track_seq == track_seq
        assert packet.sequence > packet_sequence
        assert packet.format_sha256 == plan["ready"]["tracks"][0]["format_sha256"]
        assert packet.body_bytes == len(body)
        assert packet.body_sha256 == hashlib.sha256(body).hexdigest()
        pts = packet.pts
        assert (pts.time_base_num, pts.time_base_den) == (1, _RATE)
        assert pts.sample_start == sample and pts.sample_count > 0
        assert pts.frame_index is None
        anchor = _original_audio_anchor(pts, child.audio_frames, sample)
        actual_position = Fraction(sample, _RATE)
        anchor_position = Fraction(anchor["pts"] * anchor["timebase"][0], anchor["timebase"][1])
        assert (
            anchor_position
            <= actual_position
            < anchor_position + Fraction(anchor["samples"], _RATE)
        )
        assert Fraction(packet.start.num, packet.start.den) == Fraction(sample, _RATE)
        assert Fraction(packet.end.num, packet.end.den) == Fraction(
            sample + pts.sample_count, _RATE
        )
        if container == "wav":
            with wave.open(io.BytesIO(body), "rb") as decoded:
                assert (decoded.getnchannels(), decoded.getsampwidth(), decoded.getframerate()) == (
                    1,
                    child.width,
                    _RATE,
                )
                assert decoded.getnframes() == pts.sample_count
                pcm = decoded.readframes(pts.sample_count + 1)
        else:
            pcm = body
        assert len(pcm) == pts.sample_count * child.width
        expected = child.pcm[sample * child.width : (sample + pts.sample_count) * child.width]
        assert pcm == expected
        actual_pcm.extend(pcm)
        sample += pts.sample_count
        source_seq += 1
        track_seq += 1
        packet_sequence = packet.sequence
        packets.append(packet)
        request = child.request("next")
    pytest.fail("Bounded synthetic source did not produce an original segment/source end")


@pytest.mark.parametrize("width", [2, 3, 4], ids=["pcm16", "pcm24", "pcm32"])
@pytest.mark.parametrize("container", ["raw", "wav"])
async def test_actual_child_wav_probe_and_all_selected_pcm_output_bytes(
    native_harness, width, container
):
    child = await _start(native_harness, width)
    plan = child.open_plan(container)
    end, pcm, sample, *_ = await _segment(
        child, plan, container, first_sample=0, first_source=17, first_track=9
    )
    assert pcm == child.pcm and sample == _SAMPLES
    assert end.kind == "source_eof" and end.tracks[0].state == "eof"


async def test_actual_retained_child_continues_original_samples_after_early_segment_boundary(
    native_harness,
):
    child = await _start(native_harness, 3)
    original = child.process
    first = child.open_plan("raw", end=Fraction(1, 8))
    end, pcm0, sample, source_seq, track_seq, sequence = await _segment(
        child, first, "raw", first_sample=0, first_source=17, first_track=9
    )
    assert end.kind == "segment_end" and end.tracks[0].state == "segment_boundary"
    assert sample == _SAMPLES // 2 and pcm0 == child.pcm[: sample * child.width]
    assert child.process is original and original.returncode is None
    second = child.open_plan(
        "raw", index=1, start=Fraction(1, 8), next_source=source_seq, next_track=track_seq
    )
    end, pcm1, sample, *_ = await _segment(
        child,
        second,
        "raw",
        first_sample=sample,
        first_source=source_seq,
        first_track=track_seq,
        previous_sequence=sequence,
    )
    assert end.kind == "source_eof" and end.tracks[0].state == "eof"
    assert pcm0 + pcm1 == child.pcm and sample == _SAMPLES
    assert child.process is original and len(_decoder_processes(native_harness)) == 1


async def test_valid_cancel_ack_is_followed_by_actual_original_process_exit(native_harness):
    child = await _start(native_harness, 2)
    response, body = await child.send(child.request("cancel"))
    assert response.result == "cancel_ack" and body == b""
    assert await child.receive(allow_eof=True) is None
    assert await asyncio.wait_for(child.process.wait(), _WAIT) == 0
    assert child.process.returncode == 0


@pytest.mark.parametrize(
    "fault",
    [
        "source",
        "seal",
        "child",
        "consumer",
        "profile",
        "lease_fence",
        "version",
        "noncanonical",
        "trailing_header",
        "unknown_field",
    ],
)
async def test_invalid_fd3_request_fences_original_child_without_extra_outputs(
    native_harness, fault
):
    child = await _start(native_harness, 2)
    request = child.request("open", child.open_plan("raw"))
    plan = request["plan"]
    foreign = _ref(999)
    if fault == "source":
        request["source"] = copy.deepcopy(foreign)
        plan["observation"]["source"] = copy.deepcopy(foreign)
        plan["ready"]["source"] = copy.deepcopy(foreign)
        plan["segment"]["source"] = copy.deepcopy(foreign)
    elif fault == "seal":
        request["seal"] = copy.deepcopy(foreign)
        plan["observation"]["seal"] = copy.deepcopy(foreign)
        plan["ready"]["seal"] = copy.deepcopy(foreign)
    elif fault == "child":
        request["child_owner"] = copy.deepcopy(foreign)
        plan["observation"]["child_owner"] = copy.deepcopy(foreign)
        plan["budget"]["child_owner"] = copy.deepcopy(foreign)
    elif fault == "consumer":
        request["consumer"]["consumer_id"] = "d" * 32
    elif fault == "profile":
        request["profile_sha256"] = "d" * 64
        plan["observation"]["profile_sha256"] = "d" * 64
        plan["ready"]["decoder_profile_sha256"] = "d" * 64
        plan["segment"]["profile_sha256"] = "d" * 64
    elif fault == "lease_fence":
        request["fence"]["lease_generation"] += 1
    elif fault == "version":
        request["v"] = 1
    elif fault == "unknown_field":
        request["unselected_native_authority"] = True
    if fault in {"source", "seal", "child", "consumer", "profile", "lease_fence"}:
        # These altered originals remain structurally valid closed v2 records;
        # the actual child must remember and fence against its real prior probe.
        media.media_json(_json(request), media.MediaDecodeRequestV2)
    header = _json(request)
    if fault == "noncanonical":
        header += b" "
    elif fault == "trailing_header":
        header += b"trailing-not-another-frame"
    await asyncio.wait_for(child.channel.send_chunk(_frame(header)), _WAIT)
    result = await child.receive(allow_eof=True)
    if result is not None:
        response, body = result
        assert response.result == "error" and body == b""
        assert await child.receive(allow_eof=True) is None
    code = await asyncio.wait_for(child.process.wait(), _WAIT)
    assert code > 0, "Protocol refusal must be a normal nonzero exit, not a signal crash"


def _picture_format(kind, container):
    return {
        "kind": kind,
        "container": container,
        "codec": "png" if container == "png" else "mjpeg",
        "pixel_format": "rgb24",
    }


def _known_picture(frame, color, tolerance):
    assert frame["size"] == list(_PICTURE_SIZE)
    actual = bytes.fromhex(frame["pixels"])
    expected = bytes(color) * (_PICTURE_SIZE[0] * _PICTURE_SIZE[1])
    assert len(actual) == len(expected)
    assert max(abs(left - right) for left, right in zip(actual, expected, strict=True)) <= tolerance


async def _picture_packet(
    child, response, body, plan, expected, *, source_seq, track_seq, sequence
):
    assert response.result == "packet" and response.segment_id == plan["segment"]["segment_id"]
    packet = response.packet
    assert packet.operation_id == child.binding.operation_id
    assert packet.source == child.binding.source
    assert packet.consumer == child.binding.consumer and packet.fence == child.binding.fence
    assert packet.track_id == "f" * 32 and packet.range is None
    assert packet.payload == plan["selections"][0]["format"]["kind"]
    assert packet.source_seq == packet.through_source_seq == source_seq
    assert packet.track_seq == track_seq and packet.sequence > sequence
    assert packet.continuity_id == plan["segment"]["continuity_id"]
    assert packet.format_sha256 == plan["ready"]["tracks"][0]["format_sha256"]
    assert packet.body_bytes == len(body) and packet.body_sha256 == hashlib.sha256(body).hexdigest()
    pts = packet.pts
    assert pts.pts == expected["pts"] and pts.frame_index == expected["index"]
    assert pts.sample_start is None and pts.sample_count is None
    assert [pts.time_base_num, pts.time_base_den] == expected["timebase"]
    origin = plan["ready"]["tracks"][0]["common_origin"]
    position = Fraction(pts.pts * pts.time_base_num, pts.time_base_den) - Fraction(
        origin["num"], origin["den"]
    )
    assert Fraction(packet.start.num, packet.start.den) == position
    assert packet.start == packet.end
    format_ = plan["selections"][0]["format"]
    assert body.startswith(b"\x89PNG\r\n\x1a\n" if format_["container"] == "png" else b"\xff\xd8")
    inspected = await _actual_codec(
        child.harness,
        {
            "action": "inspect",
            "source": base64.b64encode(body).decode("ascii"),
        },
    )
    assert inspected["codec"] == format_["codec"] and len(inspected["frames"]) == 1
    _known_picture(inspected["frames"][0], expected["color"], expected["tolerance"])
    return packet


def _picture_end(child, response, body, plan, packet, kind):
    assert response.result == "end" and body == b""
    assert response.segment_id == plan["segment"]["segment_id"]
    end = response.end
    assert end.segment_id == plan["segment"]["segment_id"]
    assert end.source == child.binding.source
    assert end.sealed_source.model_dump(mode="json") == child.seal
    assert end.child_owner.model_dump(mode="json") == child.child_owner
    assert end.profile_sha256 == child.profile["profile_sha256"]
    assert end.kind == kind and end.through_packet_sequence == packet.sequence
    assert len(end.tracks) == 1
    track = end.tracks[0]
    assert track.track_id == packet.track_id and track.last_pts == packet.pts
    assert track.through_source_seq == packet.source_seq
    assert track.through_track_seq == packet.track_seq
    assert track.state == ("eof" if kind == "source_eof" else "segment_boundary")


@pytest.mark.parametrize("container", ["png", "jpeg"])
@pytest.mark.parametrize("segment_end", [Fraction(0), Fraction(1)], ids=["point", "positive"])
async def test_actual_still_image_point_body_and_real_eof(native_harness, container, segment_end):
    format_ = _picture_format("image", container)
    child = await _launch(native_harness, _png_source(), [format_])
    observation = await child.probe_source()
    assert observation.probe.container == "png"
    assert len(observation.probe.tracks) == len(observation.clocks) == 1
    track, clock = observation.probe.tracks[0], observation.clocks[0]
    assert track.kind == "image" and track.codec == "png"
    assert (track.width, track.height) == _PICTURE_SIZE
    assert track.index == clock.probe_index == 0
    assert clock.first_pts == 0 and clock.provenance == "untimed_image"
    assert clock.timebase == track.timebase
    plan = child.open_plan(container, end=segment_end, format_=format_)
    assert plan["ready"]["tracks"][0]["common_origin"] == {"num": 0, "den": 1}
    response, body = await child.send(child.request("open", plan))
    packet = await _picture_packet(
        child,
        response,
        body,
        plan,
        {
            "pts": 0,
            "index": 0,
            "timebase": [clock.timebase.num, clock.timebase.den],
            "color": _COLORS[0],
            "tolerance": 0 if container == "png" else _LOSSY_TOLERANCE,
        },
        source_seq=17,
        track_seq=9,
        sequence=0,
    )
    response, body = await child.send(child.request("next"))
    _picture_end(child, response, body, plan, packet, "source_eof")


@pytest.mark.parametrize("container", ["png", "jpeg"])
async def test_actual_mp4_frame_clocks_bytes_and_retained_interval_continuation(
    native_harness, container
):
    fixture = await _actual_codec(
        native_harness,
        {
            "action": "video",
            "size": _PICTURE_SIZE,
            "colors": _COLORS,
        },
    )
    assert fixture["codec"] == "h264" and len(fixture["frames"]) == len(_COLORS)
    frames = fixture["frames"]
    for index, frame in enumerate(frames):
        assert type(frame["pts"]) is int and frame["timebase"] is not None
        _known_picture(frame, _COLORS[index], _LOSSY_TOLERANCE)
    first = frames[0]
    source_origin = Fraction(first["pts"] * first["timebase"][0], first["timebase"][1])
    for index, frame in enumerate(frames):
        assert frame["timebase"] == first["timebase"]
        assert Fraction(
            frame["pts"] * frame["timebase"][0], frame["timebase"][1]
        ) - source_origin == Fraction(index, 4)
    source = base64.b64decode(fixture["source"], validate=True)
    format_ = _picture_format("video_frame", container)
    child = await _launch(native_harness, source, [format_])
    original = child.process
    observation = await child.probe_source()
    assert observation.probe.container == "mp4"
    assert len(observation.probe.tracks) == len(observation.clocks) == 1
    track, clock = observation.probe.tracks[0], observation.clocks[0]
    assert track.kind == "video" and track.codec == "h264"
    assert (track.width, track.height) == _PICTURE_SIZE
    assert track.index == clock.probe_index == 0
    assert clock.first_pts == first["pts"] and clock.provenance == "container"
    assert [clock.timebase.num, clock.timebase.den] == first["timebase"]
    assert track.timebase == clock.timebase
    source_seq, track_seq, sequence = 17, 9, 0
    for segment_index in (0, 1):
        plan = child.open_plan(
            container,
            index=segment_index,
            start=Fraction(segment_index, 2),
            end=Fraction(segment_index + 1, 2),
            next_source=source_seq,
            next_track=track_seq,
            format_=format_,
        )
        request = child.request("open", plan)
        for frame_index in range(segment_index * 2, segment_index * 2 + 2):
            response, body = await child.send(request)
            expected = dict(
                frames[frame_index],
                index=frame_index,
                color=_COLORS[frame_index],
                tolerance=_LOSSY_TOLERANCE,
            )
            packet = await _picture_packet(
                child,
                response,
                body,
                plan,
                expected,
                source_seq=source_seq,
                track_seq=track_seq,
                sequence=sequence,
            )
            sequence = packet.sequence
            source_seq += 1
            track_seq += 1
            request = child.request("next")
        response, body = await child.send(request)
        _picture_end(
            child,
            response,
            body,
            plan,
            packet,
            "segment_end" if segment_index == 0 else "source_eof",
        )
        assert child.process is original
        if segment_index == 0:
            assert original.returncode is None
    assert len(native_harness.pools) == 1


@pytest.mark.parametrize("cut", [Fraction(1, 32000), Fraction(3, 32000)])
async def test_offgrid_adjacent_segments_own_sample_starts_without_loss_or_duplication(
    native_harness, cut
):
    child = await _start(native_harness, 2)
    original = child.process
    left = child.open_plan("raw", end=cut)
    end, prefix, sample, source_seq, track_seq, sequence = await _segment(
        child, left, "raw", first_sample=0, first_source=17, first_track=9
    )
    exact_cut = cut * _RATE
    owned = -(-exact_cut.numerator // exact_cut.denominator)
    assert end.kind == "segment_end" and end.tracks[0].state == "segment_boundary"
    assert sample == owned and prefix == child.pcm[: owned * child.width]
    # The owned final cell's actual grid end overhangs the logical cut. Sample
    # START ownership means that cell must not be dropped or repeated later.
    assert 0 < Fraction(sample, _RATE) - cut < Fraction(1, _RATE)
    assert child.process is original and original.returncode is None
    right = child.open_plan("raw", index=1, start=cut, next_source=source_seq, next_track=track_seq)
    end, suffix, sample, *_ = await _segment(
        child,
        right,
        "raw",
        first_sample=owned,
        first_source=source_seq,
        first_track=track_seq,
        previous_sequence=sequence,
    )
    assert end.kind == "source_eof" and end.tracks[0].state == "eof"
    assert sample == _SAMPLES and prefix + suffix == child.pcm
    assert child.process is original and len(_decoder_processes(native_harness)) == 1


async def test_actual_aac_forward_pts_gap_preserves_samples_clocks_and_global_pcm_indices(
    native_harness,
):
    fixture = await _actual_codec(native_harness, {}, program=_GAPPED_AUDIO_CODEC)
    frames = fixture["frames"]
    assert fixture["codec"] == "aac" and frames
    runs = []
    original_sample = 0
    for frame in frames:
        assert type(frame["pts"]) is int and frame["timebase"] == [1, _RATE]
        assert type(frame["samples"]) is int and frame["samples"] > 0
        pcm = bytes.fromhex(frame["pcm"])
        assert len(pcm) == frame["samples"] * 2
        frame["sample_start"] = original_sample
        original_sample += frame["samples"]
        if runs:
            previous_end = runs[-1]["pts"] + len(runs[-1]["pcm"]) // 2
            assert frame["pts"] >= previous_end, "Fixture must not hide backward/overlap clocks"
            if frame["pts"] == previous_end:
                runs[-1]["pcm"] += pcm
                continue
        runs.append({"pts": frame["pts"], "pcm": pcm})
    assert len(runs) >= 2, "Actual postmux AAC fixture must demonstrate a real forward PTS gap"
    first_pts = runs[0]["pts"]
    total_samples = sum(len(run["pcm"]) // 2 for run in runs)
    final_pts = runs[-1]["pts"] + len(runs[-1]["pcm"]) // 2
    assert final_pts - first_pts > total_samples
    format_ = {
        "kind": "audio",
        "container": "raw",
        "codec": "pcm_s16le",
        "rate": _RATE,
        "channels": 1,
    }
    source = base64.b64decode(fixture["source"], validate=True)
    child = await _launch(native_harness, source, [format_], width=2)
    observation = await child.probe_source()
    assert observation.probe.container == "mp4"
    assert len(observation.probe.tracks) == len(observation.clocks) == 1
    track, clock = observation.probe.tracks[0], observation.clocks[0]
    assert track.kind == "audio" and track.codec == "aac"
    assert track.rate == _RATE and track.channels == 1
    assert track.index == clock.probe_index == 0
    assert clock.first_pts == first_pts and clock.provenance == "container"
    assert (clock.timebase.num, clock.timebase.den) == (1, _RATE)
    assert track.timebase == clock.timebase
    plan = child.open_plan("raw", end=Fraction(final_pts - first_pts, _RATE))
    request = child.request("open", plan)
    global_sample = run_index = run_sample = sequence = 0
    source_seq, track_seq = 17, 9
    actual = bytearray()
    last_packet = None
    for _ in range(100):
        response, body = await child.send(request)
        if response.result == "end":
            assert last_packet is not None
            _picture_end(child, response, body, plan, last_packet, "source_eof")
            assert run_index == len(runs) and global_sample == total_samples
            assert bytes(actual) == b"".join(run["pcm"] for run in runs)
            return
        assert response.result == "packet" and response.segment_id == plan["segment"]["segment_id"]
        packet = response.packet
        assert packet.operation_id == child.binding.operation_id
        assert packet.source == child.binding.source and packet.consumer == child.binding.consumer
        assert packet.fence == child.binding.fence and packet.track_id == "f" * 32
        assert packet.payload == "audio" and packet.range is None
        assert packet.continuity_id == plan["segment"]["continuity_id"]
        assert packet.source_seq == packet.through_source_seq == source_seq
        assert packet.track_seq == track_seq and packet.sequence > sequence
        assert packet.format_sha256 == plan["ready"]["tracks"][0]["format_sha256"]
        assert (
            packet.body_bytes == len(body)
            and packet.body_sha256 == hashlib.sha256(body).hexdigest()
        )
        assert run_index < len(runs), "No invented samples/silence may follow golden source samples"
        run = runs[run_index]
        pts = packet.pts
        assert (pts.time_base_num, pts.time_base_den) == (1, _RATE)
        assert pts.sample_start == global_sample and pts.frame_index is None
        assert pts.sample_count > 0
        assert run_sample + pts.sample_count <= len(run["pcm"]) // 2, (
            "One elementary packet cannot flatten the actual source gap"
        )
        anchor = _original_audio_anchor(pts, frames, global_sample)
        actual_source_pts = run["pts"] + run_sample
        assert anchor["pts"] <= actual_source_pts < anchor["pts"] + anchor["samples"]
        assert actual_source_pts == anchor["pts"] + global_sample - anchor["sample_start"]
        assert Fraction(packet.start.num, packet.start.den) == Fraction(
            actual_source_pts - first_pts, _RATE
        )
        assert Fraction(packet.end.num, packet.end.den) == Fraction(
            actual_source_pts - first_pts + pts.sample_count, _RATE
        )
        expected = run["pcm"][run_sample * 2 : (run_sample + pts.sample_count) * 2]
        assert body == expected
        actual.extend(body)
        run_sample += pts.sample_count
        global_sample += pts.sample_count
        if run_sample == len(run["pcm"]) // 2:
            run_index += 1
            run_sample = 0
        sequence = packet.sequence
        source_seq += 1
        track_seq += 1
        last_packet = packet
        request = child.request("next")
    pytest.fail("Bounded actual AAC fixture did not reach original source EOF")


async def test_first_index_zero_open_honors_nonzero_logical_start_and_native_cursors(
    native_harness,
):
    child = await _start(native_harness, 3)
    first_sample, next_source, next_track = _SAMPLES // 2, 47, 29
    plan = child.open_plan(
        "raw", start=Fraction(1, 8), next_source=next_source, next_track=next_track
    )
    assert plan["segment"]["index"] == 0
    end, pcm, sample, source_seq, track_seq, _ = await _segment(
        child,
        plan,
        "raw",
        first_sample=first_sample,
        first_source=next_source,
        first_track=next_track,
    )
    assert pcm == child.pcm[first_sample * child.width :] and sample == _SAMPLES
    assert source_seq > next_source and track_seq > next_track
    assert end.kind == "source_eof" and end.tracks[0].state == "eof"
    assert len(_decoder_processes(native_harness)) == 1


async def _settle_original_decoder(child):
    settled = await asyncio.wait_for(child.pool.settle(child.launch), _WAIT)
    assert settled.state == "settled" and settled.bindings == (child.binding,)
    code = await asyncio.wait_for(child.process.wait(), _WAIT)
    assert type(code) is int and child.process.returncode == code


async def test_one_segment_replacement_reconstructs_prefix_like_separate_retained_reference(
    native_harness,
):
    cut = Fraction(1, 8)
    # Reference admission/profile is separate. A Source/Seal must never switch
    # its profile hash from retained_child to one_segment_child for replacement.
    reference = await _start(native_harness, 3)
    left = reference.open_plan("raw", end=cut)
    end, prefix, sample, ref_source, ref_track, sequence = await _segment(
        reference, left, "raw", first_sample=0, first_source=17, first_track=9
    )
    assert end.kind == "segment_end" and sample == _SAMPLES // 2
    right = reference.open_plan(
        "raw", index=1, start=cut, next_source=ref_source, next_track=ref_track
    )
    end, golden_suffix, sample, final_source, final_track, _ = await _segment(
        reference,
        right,
        "raw",
        first_sample=sample,
        first_source=ref_source,
        first_track=ref_track,
        previous_sequence=sequence,
    )
    assert end.kind == "source_eof" and prefix + golden_suffix == reference.pcm
    assert sample == _SAMPLES
    await _settle_original_decoder(reference)

    seal = _ref(250)
    original = await _start(
        native_harness,
        3,
        segments="one_segment_child",
        binding=process_fixtures._binding(2),
        seal=seal,
        child_owner=_ref(251),
    )
    assert original.binding.source != reference.binding.source
    assert original.seal != reference.seal
    assert original.profile["protocol"]["segments"] == "one_segment_child"
    left = original.open_plan("raw", end=cut)
    end, actual_prefix, sample, next_source, next_track, _ = await _segment(
        original, left, "raw", first_sample=0, first_source=17, first_track=9
    )
    assert end.kind == "segment_end" and end.tracks[0].state == "segment_boundary"
    assert actual_prefix == prefix and sample == _SAMPLES // 2
    assert (next_source, next_track) == (ref_source, ref_track)
    await _settle_original_decoder(original)
    previous_process = original.process
    replacement_binding = process_fixtures._binding(
        2,
        child_instance=f"{3:032x}",
        binding_sha256=hashlib.sha256(b"synthetic-replacement-native-binding-3").hexdigest(),
    )
    replacement = await _start(
        native_harness,
        3,
        segments="one_segment_child",
        binding=replacement_binding,
        seal=seal,
        child_owner=_ref(252),
        upload=original.upload,
    )
    assert previous_process.returncode is not None
    assert replacement.process is not previous_process
    assert replacement.binding.child_instance != original.binding.child_instance
    assert replacement.binding.binding_sha256 != original.binding.binding_sha256
    assert replacement.child_owner != original.child_owner
    assert replacement.binding.operation_id == original.binding.operation_id
    assert replacement.binding.source == original.binding.source
    assert replacement.binding.consumer == original.binding.consumer
    assert replacement.binding.fence == original.binding.fence
    assert replacement.seal == original.seal and replacement.upload is original.upload
    assert replacement.profile == original.profile
    right = replacement.open_plan(
        "raw", index=1, start=cut, next_source=next_source, next_track=next_track
    )
    assert right["budget"]["previous"] == left["budget_ref"]
    assert right["budget"]["child_owner"] == replacement.child_owner
    end, actual_suffix, sample, source_seq, track_seq, _ = await _segment(
        replacement,
        right,
        "raw",
        first_sample=sample,
        first_source=next_source,
        first_track=next_track,
    )
    assert actual_suffix == golden_suffix
    assert actual_prefix + actual_suffix == original.pcm and sample == _SAMPLES
    assert (source_seq, track_seq) == (final_source, final_track)
    assert end.kind == "source_eof" and end.tracks[0].state == "eof"
    assert len(_decoder_processes(native_harness)) == 3
