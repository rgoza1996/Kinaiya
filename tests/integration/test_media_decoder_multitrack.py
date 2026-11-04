"""Source-aware same-kind mapping through the actual owned decoder process.

The author also wrote the decoder child. Postmux AAC samples and clocks are
inspected independently; no decoder-private method supplies the golden data.
The existing native process/spool harness is reused read-only. Its synthetic
references and dependency callbacks are not complete Session authorization,
runtime qualification, publication settlement or public cleanup receipts.
"""

import asyncio
import base64
import copy
import hashlib
import importlib.util
import io
import wave
from fractions import Fraction
from pathlib import Path

import pytest

from packages.contracts import media


def _child_helpers():
    # Path loading works when this module collects alone, without another test
    # adding the integration directory to sys.path or replacing a helper file.
    path = Path(__file__).with_name("test_media_decoder_child.py")
    spec = importlib.util.spec_from_file_location("multitrack_child_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helpers = _child_helpers()
native_harness = helpers.native_harness
_WAIT = helpers._WAIT
_RATE = 16000
_TWO_AUDIO = """
import base64, io, json, math, struct, sys
from fractions import Fraction
import av
if av.__version__ != '17.1.0':
    raise SystemExit('Actual selected PyAV17.1.0 is required')
json.load(sys.stdin)
sink = io.BytesIO()
with av.open(sink, mode='w', format='mp4') as output:
    streams = [output.add_stream('aac', rate=16000) for _ in range(2)]
    for stream in streams:
        stream.layout = 'mono'
        stream.bit_rate = 64000
        stream.time_base = Fraction(1, 16000)
    blocks, offsets, frequencies = (5, 7), (0, 2048), (330, 880)
    for index in range(max(blocks)):
        for lane, stream in enumerate(streams):
            if index >= blocks[lane]:
                continue
            frame = av.AudioFrame(format='fltp', layout='mono', samples=1024)
            frame.sample_rate = 16000
            frame.pts = offsets[lane] + index * 1024
            frame.time_base = Fraction(1, 16000)
            values = [0.28 * math.sin(2 * math.pi * frequencies[lane]
                                     * (index * 1024 + sample) / 16000)
                      for sample in range(1024)]
            raw = struct.pack('<1024f', *values)
            frame.planes[0].update(raw + bytes(frame.planes[0].buffer_size - len(raw)))
            for packet in stream.encode(frame):
                output.mux(packet)
    for stream in streams:
        for packet in stream.encode(None):
            output.mux(packet)
source = sink.getvalue()
with av.open(io.BytesIO(source), mode='r') as container:
    assert len(container.streams) == len(container.streams.audio) == 2
    indices = [stream.index for stream in container.streams.audio]
tracks = []
for index in indices:
    # Each inspection starts from the completed mux, not pre-encode samples or
    # requested timestamps. AAC padding/edit-list behavior is observed honestly.
    with av.open(io.BytesIO(source), mode='r') as container:
        stream = next(stream for stream in container.streams.audio if stream.index == index)
        assert stream.codec_context.name == 'aac' and stream.time_base is not None
        frames, sample_start = [], 0
        for frame in container.decode(stream):
            assert frame.format.name in ('fltp', 'flt') and frame.layout.name == 'mono'
            assert frame.sample_rate == 16000 and type(frame.pts) is int
            assert frame.time_base is not None and frame.samples > 0
            assert frame.time_base == stream.time_base
            values = struct.unpack('=%df' % frame.samples,
                                   bytes(frame.planes[0])[:frame.samples * 4])
            assert all(math.isfinite(value) for value in values)
            # Scalar float->PCM16 conversion is independent of the candidate's
            # resampler, packet splitting, stream merge and source cursors.
            pcm = b''.join(struct.pack('<h', max(-32768, min(32767, round(value * 32768))))
                           for value in values)
            frames.append({'pts': frame.pts, 'samples': frame.samples,
                           'sample_start': sample_start, 'pcm': pcm.hex(),
                           'timebase': [frame.time_base.numerator,
                                        frame.time_base.denominator]})
            sample_start += frame.samples
        tracks.append({'index': index, 'codec': stream.codec_context.name,
                       'timebase': [stream.time_base.numerator, stream.time_base.denominator],
                       'frames': frames})
json.dump({'source': base64.b64encode(source).decode(), 'tracks': tracks},
          sys.stdout, sort_keys=True)
"""


def _position(frame):
    return Fraction(frame["pts"] * frame["timebase"][0], frame["timebase"][1])


def _inspect_golden(report):
    tracks = report["tracks"]
    assert len(tracks) == len({track["index"] for track in tracks}) == 2
    golden = {}
    for track in tracks:
        assert track["codec"] == "aac" and track["frames"]
        sample = 0
        previous_end = None
        for frame in track["frames"]:
            assert type(frame["pts"]) is int and frame["timebase"] == track["timebase"]
            assert type(frame["samples"]) is int and frame["samples"] > 0
            assert frame["sample_start"] == sample
            assert len(bytes.fromhex(frame["pcm"])) == frame["samples"] * 2
            if previous_end is not None:
                assert _position(frame) == previous_end
            previous_end = _position(frame) + Fraction(frame["samples"], _RATE)
            sample += frame["samples"]
        golden[track["index"]] = {
            "frames": track["frames"],
            "first": _position(track["frames"][0]),
            "end": previous_end,
            "samples": sample,
            "pcm": b"".join(bytes.fromhex(frame["pcm"]) for frame in track["frames"]),
            "timebase": track["timebase"],
        }
    first, second = (golden[track["index"]] for track in tracks)
    assert first["first"] != second["first"], "Actual postmux starts must differ"
    assert first["end"] != second["end"], "Actual stream exhaustion must differ"
    shared = min(len(first["pcm"]), len(second["pcm"]))
    assert first["pcm"][:shared] != second["pcm"][:shared], "Actual source content must differ"
    return golden


def _plan(child, observation, golden):
    origin = min(value["first"] for value in golden.values())
    end = max(value["end"] for value in golden.values()) - origin
    plan = child.open_plan("raw", end=end, next_source=1, next_track=1)
    tracks, selections = [], []
    clocks = {clock.probe_index: clock for clock in observation.clocks}
    containers = ("raw", "wav")
    for ordinal, probe in enumerate(observation.probe.tracks):
        expected = golden[probe.index]
        clock = clocks[probe.index]
        assert probe.kind == "audio" and probe.codec == "aac"
        assert probe.rate == _RATE and probe.channels == 1
        assert [probe.timebase.num, probe.timebase.den] == expected["timebase"]
        assert clock.first_pts == expected["frames"][0]["pts"]
        assert clock.timebase == probe.timebase and clock.provenance == "container"
        format_ = {
            "kind": "audio",
            "container": containers[ordinal],
            "codec": "pcm_s16le",
            "rate": _RATE,
            "channels": 1,
        }
        track = {
            "track_id": f"{600 + ordinal:032x}",
            "source_id": child.binding.source_id,
            "kind": "audio",
            "format_sha256": media.media_digest("Format", format_),
            "timebase": probe.timebase.model_dump(mode="json"),
            "common_origin": {"num": origin.numerator, "den": origin.denominator},
            "clock_provenance": "container",
            "command_lane": False,
        }
        tracks.append(track)
        selections.append(
            {
                "probe_index": probe.index,
                "track": copy.deepcopy(track),
                "format": format_,
            }
        )
    plan["ready"]["tracks"] = tracks
    plan["selections"] = list(reversed(selections))
    plan["segment"]["track_ids"] = [track["track_id"] for track in tracks]
    plan["cursors"] = [{"track_id": track["track_id"], "next_seq": 1} for track in reversed(tracks)]
    assert [value["probe_index"] for value in plan["selections"]] == list(
        reversed([probe.index for probe in observation.probe.tracks])
    )
    assert len({track["track_id"] for track in tracks}) == 2
    assert len({track["format_sha256"] for track in tracks}) == 2
    return plan, origin


async def test_two_actual_audio_tracks_keep_reversed_native_mapping_and_exact_pcm(native_harness):
    try:
        helpers._interpreter()
    except pytest.skip.Exception:
        pytest.fail("This execution oracle requires the actual selected PyAV17.1.0 runtime")
    report = await helpers._actual_codec(native_harness, {}, program=_TWO_AUDIO)
    golden = _inspect_golden(report)
    formats = [
        {
            "kind": "audio",
            "container": container,
            "codec": "pcm_s16le",
            "rate": _RATE,
            "channels": 1,
        }
        for container in ("raw", "wav")
    ]
    source = base64.b64decode(report["source"], validate=True)
    child = await helpers._launch(native_harness, source, formats, width=2)
    original = child.process
    observation = await child.probe_source()
    assert observation.probe.container == "mp4"
    assert len(observation.probe.tracks) == len(observation.clocks) == 2
    assert {track.index for track in observation.probe.tracks} == set(golden)
    plan, origin = _plan(child, observation, golden)
    by_id = {value["track"]["track_id"]: value for value in plan["selections"]}
    samples = {track_id: 0 for track_id in by_id}
    track_seq = {track_id: 1 for track_id in by_id}
    actual = {track_id: bytearray() for track_id in by_id}
    last = {}
    source_seq = 1
    previous_position = None
    request = child.request("open", plan)
    # Every valid audio packet owns at least one sample; this finite golden
    # bounds progress without requiring the implementation's packet chunk size.
    total_samples = sum(value["samples"] for value in golden.values())
    for _ in range(total_samples + 1):
        response, body = await child.send(request)
        assert child.process is original
        assert response.segment_id == plan["segment"]["segment_id"]
        if response.result == "end":
            end = response.end
            assert not body and end.kind == "source_eof"
            assert end.source == child.binding.source
            assert end.sealed_source.model_dump(mode="json") == child.seal
            assert end.child_owner.model_dump(mode="json") == child.child_owner
            assert end.profile_sha256 == child.profile["profile_sha256"]
            assert end.through_packet_sequence == source_seq - 1
            assert {track.track_id for track in end.tracks} == set(by_id)
            for track in end.tracks:
                index = by_id[track.track_id]["probe_index"]
                assert track.state == "eof"
                assert samples[track.track_id] == golden[index]["samples"]
                assert bytes(actual[track.track_id]) == golden[index]["pcm"]
                assert track.through_track_seq == track_seq[track.track_id] - 1
                assert track.through_source_seq == last[track.track_id].source_seq
                assert track.last_pts == last[track.track_id].pts
            break
        assert response.result == "packet"
        packet = response.packet
        assert packet.track_id in by_id
        selection = by_id[packet.track_id]
        expected = golden[selection["probe_index"]]
        assert packet.operation_id == child.binding.operation_id
        assert packet.source == child.binding.source and packet.consumer == child.binding.consumer
        assert packet.fence == child.binding.fence
        assert packet.payload == "audio" and packet.range is None
        assert packet.continuity_id == plan["segment"]["continuity_id"]
        assert packet.format_sha256 == selection["track"]["format_sha256"]
        assert packet.source_seq == packet.through_source_seq == packet.sequence == source_seq
        assert packet.track_seq == track_seq[packet.track_id]
        assert packet.body_bytes == len(body)
        assert packet.body_sha256 == hashlib.sha256(body).hexdigest()
        pts = packet.pts
        sample = samples[packet.track_id]
        assert pts.sample_start == sample and pts.sample_count > 0
        assert pts.frame_index is None
        assert [pts.time_base_num, pts.time_base_den] == expected["timebase"]
        anchor = helpers._original_audio_anchor(pts, expected["frames"], sample)
        position = expected["first"] + Fraction(sample, _RATE)
        assert (
            _position(anchor) <= position < _position(anchor) + Fraction(anchor["samples"], _RATE)
        )
        assert Fraction(packet.start.num, packet.start.den) == position - origin
        assert Fraction(packet.end.num, packet.end.den) == (
            position - origin + Fraction(pts.sample_count, _RATE)
        )
        if previous_position is not None:
            assert Fraction(packet.start.num, packet.start.den) >= previous_position
        previous_position = Fraction(packet.start.num, packet.start.den)
        if selection["format"]["container"] == "wav":
            with wave.open(io.BytesIO(body), "rb") as decoded:
                assert (decoded.getnchannels(), decoded.getsampwidth(), decoded.getframerate()) == (
                    1,
                    2,
                    _RATE,
                )
                assert decoded.getnframes() == pts.sample_count
                pcm = decoded.readframes(pts.sample_count + 1)
        else:
            pcm = body
        assert len(pcm) == pts.sample_count * 2
        assert pcm == expected["pcm"][sample * 2 : (sample + pts.sample_count) * 2]
        actual[packet.track_id].extend(pcm)
        samples[packet.track_id] += pts.sample_count
        track_seq[packet.track_id] += 1
        source_seq += 1
        last[packet.track_id] = packet
        request = child.request("next")
    else:
        pytest.fail("Finite two-track source did not produce actual all-track EOF")
    assert helpers._decoder_processes(native_harness) == [original]
    # Source EOF is not process retirement. Join the existing pool's actual
    # original close and the captured Process, whether EOF retained it or not.
    # Harness teardown still owns/reaps both inspector and decoder on failure.
    await asyncio.wait_for(child.pool.close(), _WAIT)
    assert await asyncio.wait_for(original.wait(), _WAIT) is not None
    assert original.returncode is not None
