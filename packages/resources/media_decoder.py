"""Bounded PyAV17 FD3/FD4 elementary decoder; not an admission/cleanup owner.

Run only inside the native owner's sealed, networkless runtime. FD3 is the
original Unix control socket and FD4 the original read-only regular source.
The parent must reserve and resolve the original probe/segment budget before
launch. Child measurements include probe, hashing, reconstruction and blocked
I/O; they neither grant a budget nor establish physical retirement.

V2 emits one elementary packet per source sequence, with packet.sequence equal
to source_seq. PCM sample indices count the selected output PCM track. Its PTS
is an observed ORIGINAL input-frame anchor; output timestamps locate samples
within that anchor interval, never replace the original clock. Unprovable
timing is refused. No missing PTS, frame-index clock or implicit transform is
manufactured. The general v1 wire grammar is unchanged.

Finite v2 segments own whole PCM samples by presentation START in [start,end).
Their last sample cell can overhang a rational end by less than one sample;
the exact unselected suffix remains for the adjacent segment. Zero-width
segments select a still/video point exactly at that position. Recorded forward
PTS gaps restart the resampler without silence; backward/overlapping audio is
unsupported, rather than confused with native capture/seek/reset authority.

Pending wire bodies have allocation-owned credits, reserved before producing
bytes and shared by replay/scatter views. At most eight lazy per-track heads
retain actual decoder/resampler frames or original AV packets as codec scratch;
this scratch, codec internals and construction temporaries are under RLIMIT_AS,
not the wire payload counter. Neither mechanism is a heap/RSS guarantee.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib
import io
import math
import os
import resource
import signal
import socket
import stat
import struct
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction

from packages.contracts import media as m
from services.common.media_decoder_wire import (
    decoder_request_v2_json,
    decoder_response_v2_bytes,
)


class DecoderError(ValueError):
    def __init__(self, code="unsupported"):
        self.code = code
        super().__init__("Invalid native media decode")


def _fail(code="unsupported"):
    raise DecoderError(code)


def _float_pcm16(planes, samples, channels, planar):
    """Canonical float32 identity conversion, independent of SIMD rounding.

    Codec/construction scratch remains under the original child RLIMIT_AS.
    This neither resamples nor changes layout, timestamps or sample counts.
    """
    if (
        type(samples) is not int
        or samples < 1
        or type(channels) is not int
        or channels not in (1, 2)
        or type(planar) is not bool
        or type(planes) is not list
        or len(planes) != (channels if planar else 1)
    ):
        _fail()
    size = samples * channels * 2
    if size > m.MAX_MEDIA_BODY_BYTES:
        _fail("bound")
    views = [memoryview(plane).cast("B") for plane in planes]
    required = samples * 4 * (1 if planar else channels)
    if any(len(view) < required for view in views):
        _fail()
    output = bytearray(size)
    for sample in range(samples):
        for channel in range(channels):
            plane = views[channel] if planar else views[0]
            index = sample if planar else sample * channels + channel
            value = struct.unpack_from("<f", plane, index * 4)[0]
            if not math.isfinite(value):
                _fail()
            quantized = max(-32768, min(32767, round(value * 32768)))
            struct.pack_into("<h", output, (sample * channels + channel) * 2, quantized)
    return output


def _fraction(value):
    if value is None:
        _fail()
    result = Fraction(value)
    if (
        result.denominator <= 0
        or max(abs(result.numerator), result.denominator) > m.MAX_MEDIA_INTEGER
    ):
        _fail("bound")
    return result


def _q(value):
    value = _fraction(value)
    if value < 0:
        _fail("conflict")
    return m.MediaQ(num=value.numerator, den=value.denominator)


def _pin(value):
    return m.media_bytes(value)


class _Source(io.RawIOBase):
    """An independent bounded pread cursor borrowing, never closing, FD4."""

    def __init__(self, fd, identity, chunk, check):
        self.fd, self.identity, self.chunk, self.check = fd, identity, chunk, check
        self.position = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=os.SEEK_SET):
        self.check()
        if type(offset) is not int:
            _fail("bad_packet")
        base = {os.SEEK_SET: 0, os.SEEK_CUR: self.position, os.SEEK_END: self.identity[2]}
        if whence not in base or not 0 <= base[whence] + offset <= self.identity[2]:
            _fail("bad_packet")
        self.position = base[whence] + offset
        return self.position

    def read(self, size=-1):
        self.check()
        if self.closed or type(size) is not int:
            _fail("bad_packet")
        size = self.chunk if size < 0 else min(size, self.chunk)
        value = os.pread(self.fd, min(size, self.identity[2] - self.position), self.position)
        self.position += len(value)
        self.check()
        return value

    def readinto(self, buffer):
        value = self.read(len(buffer))
        buffer[: len(value)] = value
        return len(value)


@dataclass
class _Track:
    stream: object
    selection: object
    clock: object
    sample: int = 0
    frame: int = 0
    next_seq: int = 1
    last_pts: object = None
    through_source: int = 0
    anchors: deque = field(default_factory=deque)
    times: deque = field(default_factory=deque)
    frames_segment: int = 0
    next_video_time: object = None
    resampler: object = None
    output_end: object = None
    input_end: object = None
    decoded: object = None
    eof: bool = False
    first_seen: bool = False


@dataclass
class _Piece:
    track: _Track
    start: Fraction
    end: Fraction
    pts: object
    payload: object
    sample_width: int = 0


@dataclass
class _Boundary:
    start: Fraction
    point: bool = False


class _WireAllocation:
    """One charged immutable wire allocation; aliases never charge twice."""

    def __init__(self, owner, reserved):
        if (
            reserved < 0
            or owner.wire_pending + reserved > owner.profile.limits.wire.total_pending_bytes
        ):
            _fail("bound")
        self.owner, self.reserved, self.raw = owner, reserved, None
        owner.wire_pending += reserved

    def populate(self, factory, expected):
        raw = factory()
        if type(raw) is not bytes or len(raw) != expected or len(raw) > self.reserved:
            _fail("bound")
        self.owner.wire_pending -= self.reserved - len(raw)
        self.reserved, self.raw = len(raw), raw
        return self

    def __del__(self):
        if hasattr(self, "owner"):
            self.owner.wire_pending -= self.reserved


@dataclass(frozen=True)
class _WireBody:
    allocations: tuple

    def __len__(self):
        return sum(len(value.raw) for value in self.allocations)

    def parts(self):
        for value in self.allocations:
            yield memoryview(value.raw)

    def digest(self):
        result = hashlib.sha256()
        for part in self.parts():
            result.update(part)
        return result.hexdigest()


@dataclass(frozen=True)
class _PCM:
    """A lazy slice of original resampler scratch, not a copied wire body."""

    plane: object
    first: int
    count: int
    native_width: int
    width: int
    format: object

    def split(self, count):
        return (
            _PCM(self.plane, self.first, count, self.native_width, self.width, self.format),
            _PCM(
                self.plane,
                self.first + count,
                self.count - count,
                self.native_width,
                self.width,
                self.format,
            ),
        )

    def materialize(self, owner):
        fmt = self.format
        size = self.count * self.width * fmt.channels
        # Credit precedes either the byte copy or PCM24 packing. No full
        # resampler-output copy or joined WAV/body copy is retained.
        allocation = _WireAllocation(owner, size)
        header = _WireAllocation(owner, 44) if fmt.container == "wav" else None
        offset = self.first * self.native_width * fmt.channels
        if self.width == self.native_width:
            allocation.populate(lambda: bytes(self.plane[offset : offset + size]), size)
        else:
            allocation.populate(
                lambda: bytes(
                    self.plane[offset + sample * 4 + lane]
                    for sample in range(self.count * fmt.channels)
                    for lane in (1, 2, 3)
                ),
                size,
            )
        if fmt.container == "raw":
            return _WireBody((allocation,))
        header.populate(
            lambda: struct.pack(
                "<4sI4s4sIHHIIHH4sI",
                b"RIFF",
                36 + size,
                b"WAVE",
                b"fmt ",
                16,
                1,
                fmt.channels,
                fmt.rate,
                fmt.rate * fmt.channels * self.width,
                fmt.channels * self.width,
                self.width * 8,
                b"data",
                size,
            ),
            44,
        )
        return _WireBody((header, allocation))


@dataclass(frozen=True)
class _Image:
    frame: object
    width: int
    height: int
    format: object

    def materialize(self, owner):
        fmt = self.format
        maximum = min(
            8388608,
            owner.profile.limits.wire.image_bytes,
            owner.profile.limits.wire.body_bytes,
            owner.profile.limits.wire.consumer_bytes,
        )
        # Only the selected head is encoded; reserve before producing its wire
        # candidate. Decoder/codec frame storage remains separate AS scratch.
        allocation = _WireAllocation(
            owner, min(maximum, owner.profile.limits.wire.total_pending_bytes - owner.wire_pending)
        )
        rgb = self.frame.reformat(width=self.width, height=self.height, format="rgb24")
        codec = owner.av.CodecContext.create(fmt.codec, "w")
        codec.width, codec.height = self.width, self.height
        codec.pix_fmt = "rgb24" if fmt.codec == "png" else "yuvj444p"
        codec.time_base = Fraction(1, 1)
        encoded = rgb if fmt.codec == "png" else rgb.reformat(format="yuvj444p")
        packets = [*codec.encode(encoded), *codec.encode(None)]
        if len(packets) != 1:
            _fail()
        if packets[0].size > allocation.reserved:
            _fail("bound")
        allocation.populate(lambda: bytes(packets[0]), packets[0].size)
        return _WireBody((allocation,))


@dataclass(frozen=True)
class _Response:
    header: bytes
    body: _WireBody

    def send(self, channel):
        channel.sendall(struct.pack(">I", len(self.header)))
        channel.sendall(self.header)
        for part in self.body.parts():
            channel.sendall(part)


class Decoder:
    """One original source/profile, a probe, and finite explicit segments."""

    def __init__(self, channel, source_fd=4):
        self.channel, self.source_fd = channel, source_fd
        info = os.fstat(source_fd)
        flags = fcntl.fcntl(source_fd, fcntl.F_GETFL)
        if (
            not stat.S_ISREG(info.st_mode)
            or flags & os.O_ACCMODE != os.O_RDONLY
            or flags & os.O_PATH
        ):
            _fail("bad_packet")
        self.identity = self._identity(info)
        self.request_identity = None
        self.profile = self.observation = self.container = self.plan = None
        self.containers = []
        self.tracks = {}
        self.iterator = self.pieces = None
        self.pending = None
        self.eof = self.fenced = False
        self.last_request = self.last_response = None
        self.wire_pending = 0
        self.request_seq = 0
        self.source_seq = 1
        self.packet_through = 0
        self.cpu_start = self.wall_start = None
        self.segment_cpu = self.segment_wall = 0.0
        self.cpu_spent = self.wall_spent = 0.0
        self._armed = False
        self.cpu_ceiling = resource.getrlimit(resource.RLIMIT_CPU)[0]
        alarm, _ = signal.getitimer(signal.ITIMER_REAL)
        self.wall_ceiling = None if alarm <= 0 else time.monotonic() + alarm

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _check(self):
        if self._identity(os.fstat(self.source_fd)) != self.identity:
            _fail("conflict")
        if self.profile is None:
            return
        cpu = time.process_time() - self.segment_cpu + self.cpu_spent
        wall = time.monotonic() - self.segment_wall + self.wall_spent
        if (
            cpu * 1000 >= self.profile.limits.decode.cpu_ms
            or wall * 1000 >= self.profile.limits.decode.wall_ms
        ):
            _fail("bound")

    def _arm(self):
        self._check()
        remaining = self.profile.limits.decode.wall_ms / 1000 - (
            time.monotonic() - self.segment_wall + self.wall_spent
        )
        if self.wall_ceiling is not None:
            remaining = min(remaining, self.wall_ceiling - time.monotonic())
        if remaining <= 0:
            _fail("bound")
        self.channel.settimeout(remaining)
        signal.setitimer(signal.ITIMER_REAL, remaining)
        # The kernel CPU soft limit remains cumulative over this original PID.
        # Raising a later segment's soft boundary does not reset CPU consumed.
        _, hard = resource.getrlimit(resource.RLIMIT_CPU)
        remaining_cpu = self.profile.limits.decode.cpu_ms / 1000 - (
            time.process_time() - self.segment_cpu + self.cpu_spent
        )
        soft = max(1, math.ceil(time.process_time() + remaining_cpu))
        if self.cpu_ceiling != resource.RLIM_INFINITY:
            soft = min(soft, self.cpu_ceiling)
        if hard != resource.RLIM_INFINITY and soft > hard:
            _fail("bound")
        resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))
        self._armed = True

    def _limits(self, profile):
        limit = profile.limits
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        address = limit.decode.address_space_bytes
        if soft != resource.RLIM_INFINITY:
            address = min(address, soft)
        if hard != resource.RLIM_INFINITY:
            address = min(address, hard)
        resource.setrlimit(resource.RLIMIT_AS, (address, address))
        cpus = sorted(os.sched_getaffinity(0))
        if len(cpus) < limit.decode.cpu_affinity_count:
            _fail("unavailable")
        os.sched_setaffinity(0, cpus[: limit.decode.cpu_affinity_count])
        if not 0 < self.identity[2] <= limit.source.file_bytes:
            _fail("bound")

    def _hash(self):
        digest, offset = hashlib.sha256(), 0
        while offset < self.identity[2]:
            self._check()
            raw = os.pread(
                self.source_fd,
                min(
                    m.MAX_MEDIA_BODY_BYTES,
                    self.profile.limits.source.upload_chunk_bytes,
                    self.identity[2] - offset,
                ),
                offset,
            )
            if not raw:
                _fail("conflict")
            digest.update(raw)
            offset += len(raw)
        self._check()
        return digest.hexdigest()

    def _open_container(self):
        source = _Source(
            self.source_fd,
            self.identity,
            min(self.profile.limits.source.upload_chunk_bytes, m.MAX_MEDIA_BODY_BYTES),
            self._check,
        )
        return self.av.open(
            source,
            mode="r",
            format=self.demuxer,
            metadata_errors="strict",
            options={"protocol_whitelist": "", "format_whitelist": self.demuxer},
            io_open=lambda *_: _fail(),
        )

    def _webm(self):
        """Read only the actual bounded EBML header's closed DocType field."""
        maximum = min(
            self.identity[2],
            self.profile.limits.decode.probe_metadata_bytes,
            m.MAX_MEDIA_BODY_BYTES,
        )
        raw = os.pread(self.source_fd, maximum, 0)

        def vint(offset, *, identifier=False):
            if offset >= len(raw) or raw[offset] == 0:
                _fail()
            width = 9 - raw[offset].bit_length()
            if width > (4 if identifier else 8) or offset + width > len(raw):
                _fail()
            value = int.from_bytes(raw[offset : offset + width], "big")
            if not identifier:
                value &= (1 << (7 * width)) - 1
                if value == (1 << (7 * width)) - 1:
                    _fail()
            return value, offset + width

        tag, offset = vint(0, identifier=True)
        if tag != 0x1A45DFA3:
            _fail()
        size, offset = vint(offset)
        boundary = offset + size
        if boundary > len(raw):
            _fail("bound")
        found = False
        while offset < boundary:
            tag, offset = vint(offset, identifier=True)
            size, offset = vint(offset)
            if offset + size > boundary:
                _fail()
            if tag == 0x4282:
                if found or raw[offset : offset + size] != b"webm":
                    _fail()
                found = True
            offset += size
        if not found:
            _fail()

    def _container_kind(self):
        head = os.pread(self.source_fd, 32, 0)
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png", "png_pipe"
        if head.startswith(b"\xff\xd8\xff"):
            return "jpeg", "jpeg_pipe"
        if head[:4] in (b"RIFF", b"RF64", b"BW64") and head[8:12] == b"WAVE":
            return "wav", "wav"
        if head.startswith(b"\x1aE\xdf\xa3"):
            self._webm()
            return "webm", "matroska"
        # ISO-BMFF may place free/other boxes before ftyp. Inspect bounded box
        # headers through pread/seek; never copy intervening mdat bytes to RAM.
        offset = inspected = 0
        while offset + 8 <= self.identity[2]:
            self._check()
            box = os.pread(self.source_fd, 16, offset)
            size = int.from_bytes(box[:4], "big")
            header_size = 8
            if size == 1:
                if len(box) < 16:
                    _fail()
                size = int.from_bytes(box[8:16], "big")
                header_size = 16
            elif size == 0:
                size = self.identity[2] - offset
            inspected += header_size
            if inspected > self.profile.limits.decode.probe_metadata_bytes:
                _fail("bound")
            if size < header_size or offset + size > self.identity[2]:
                _fail()
            if box[4:8] == b"ftyp":
                if size < header_size + 8:
                    _fail()
                return "mp4", "mov"
            offset += size
        _fail()

    def _codec(self, stream, kind):
        names = {
            "audio": ("pcm_s16le", "pcm_s24le", "pcm_s32le", "aac", "opus"),
            "image": ("png", "mjpeg"),
            "video": ("png", "mjpeg", "h264", "hevc", "vp9", "av1"),
        }[kind]
        actual = stream.codec_context.codec.id
        for name in names:
            try:
                if self.av.Codec(name, "r").id == actual:
                    return name
            except (ValueError, LookupError):
                continue
        _fail()

    def _geometry(self, width, height):
        limits = self.profile.limits.decode
        if type(width) is not int or type(height) is not int or min(width, height) <= 0:
            _fail()
        if max(width, height) > limits.max_edge or width * height > limits.pixels_per_frame:
            _fail("bound")

    def _probe(self, request):
        if self.profile is not None:
            _fail("conflict")
        self.profile = request.profile
        self.segment_cpu = self.cpu_start = time.process_time()
        self.segment_wall = self.wall_start = time.monotonic()
        self._limits(self.profile)
        self._arm()
        self.av = importlib.import_module("av")
        if self.av.__version__ != "17.1.0":
            _fail("unavailable")
        container_kind, self.demuxer = self._container_kind()
        digest = self._hash()
        tracks, clocks = [], []
        with self._open_container() as container:
            streams = list(container.streams)
            if not 1 <= len(streams) <= min(8, self.profile.limits.decode.track_limit):
                _fail("bound")
            metadata_bytes = 0
            for metadata in (container.metadata, *(stream.metadata for stream in streams)):
                for key, value in metadata.items():
                    for text in (key, value):
                        if type(text) is not str:
                            _fail()
                        remaining = self.profile.limits.decode.probe_metadata_bytes - metadata_bytes
                        if len(text) > remaining:
                            _fail("bound")
                        metadata_bytes += len(text.encode("utf-8", "strict"))
                        if metadata_bytes > self.profile.limits.decode.probe_metadata_bytes:
                            _fail("bound")
            for stream in streams:
                self._check()
                if stream.type not in ("audio", "video"):
                    _fail()
                kind = "image" if container_kind in ("png", "jpeg") else stream.type
                codec = self._codec(stream, kind)
                tb = _fraction(stream.time_base)
                if tb <= 0:
                    _fail()
                context = stream.codec_context
                duration = None if stream.duration is None else _q(Fraction(stream.duration) * tb)
                if (
                    duration is not None
                    and Fraction(duration.num * 1000, duration.den)
                    > self.profile.limits.source.file_duration_ms
                ):
                    _fail("bound")
                if kind == "audio":
                    rate, channels = context.sample_rate, len(context.layout.channels)
                    if type(rate) is not int or rate <= 0 or channels <= 0:
                        _fail()
                    width = height = None
                else:
                    rate = channels = None
                    width, height = context.width, context.height
                    self._geometry(width, height)
                    average = stream.average_rate
                    if average is not None and _fraction(average) > Fraction(
                        self.profile.limits.decode.frame_rate.num,
                        self.profile.limits.decode.frame_rate.den,
                    ):
                        _fail("bound")
                tracks.append(
                    m.MediaProbeTrack(
                        index=stream.index,
                        kind=kind,
                        codec=codec,
                        rate=rate,
                        channels=channels,
                        width=width,
                        height=height,
                        timebase=_q(tb),
                        duration=duration,
                    )
                )
        # Decode each first frame independently, without retaining potentially
        # hours of another stream while a later-starting track is discovered.
        for track in tracks:
            with self._open_container() as container:
                stream = next((s for s in container.streams if s.index == track.index), None)
                if stream is None:
                    _fail("conflict")
                frame = next(container.decode(stream), None)
                if frame is None:
                    _fail()
                self._frame_clock(frame, track)
                pts = 0 if track.kind == "image" else frame.pts
                clocks.append(
                    m.MediaProbeClock(
                        probe_index=track.index,
                        first_pts=pts,
                        timebase=track.timebase,
                        provenance="untimed_image" if track.kind == "image" else "container",
                    )
                )
        probe = m.MediaProbeResult(
            v=1,
            source_sha256=digest,
            input_bytes=self.identity[2],
            container=container_kind,
            tracks=tracks,
        )
        self.observation = m.MediaProbeObservation(
            v=2,
            source=request.source,
            seal=request.seal,
            child_owner=request.child_owner,
            profile_sha256=request.profile_sha256,
            probe=probe,
            clocks=clocks,
        )
        return self._response(request, "probe", probe=self.observation)

    def _frame_clock(self, frame, probe):
        if probe.kind == "image":
            return Fraction(0)
        if type(frame.pts) is not int or _fraction(frame.time_base) != Fraction(
            probe.timebase.num, probe.timebase.den
        ):
            _fail()
        # Absolute products can exceed the wire's integer range while both
        # their original PTS and rebased position are representable.
        value = Fraction(frame.pts) * Fraction(probe.timebase.num, probe.timebase.den)
        if abs(frame.pts) > m.MAX_MEDIA_INTEGER:
            _fail("bound")
        return value

    def _open(self, request):
        plan = request.plan
        if self.observation is None or self.eof or _pin(plan.observation) != _pin(self.observation):
            _fail("conflict")
        self._validate_plan(plan)
        if self.plan is not None:
            old = self.plan.segment
            if (
                self.iterator is None
                or plan.segment.index != old.index + 1
                or plan.segment.start != old.end
                or plan.segment.continuity_id != old.continuity_id
            ):
                _fail("conflict")
            if self._segment_active:
                _fail("conflict")
            if self.profile.protocol.segments != "retained_child":
                _fail("conflict")
            if plan.budget.previous != self.plan.budget_ref:
                _fail("conflict")
            if plan.next_source_seq != self.source_seq:
                _fail("conflict")
            by_id = {c.track_id: c.next_seq for c in plan.cursors}
            if any(
                by_id[t.selection.track.track_id] != t.next_seq
                or _pin(next(s for s in plan.selections if s.probe_index == i)) != _pin(t.selection)
                for i, t in self.tracks.items()
            ):
                _fail("conflict")
            # Reject future clock baselines only. Their native admission,
            # cumulative charge and truthful reconciliation are parent-owned;
            # this comparison cannot authenticate them or detect under-report.
            budget = plan.budget
            if budget.cpu_baseline_ms > int(
                time.process_time() * 1000
            ) or budget.wall_baseline_ms > int(time.monotonic() * 1000):
                _fail("conflict")
            self.segment_cpu, self.segment_wall = time.process_time(), time.monotonic()
            self.cpu_spent, self.wall_spent = (
                budget.cpu_spent_ms / 1000,
                budget.wall_spent_ms / 1000,
            )
        else:
            if self._hash() != self.observation.probe.source_sha256:
                _fail("conflict")
            clocks = {c.probe_index: c for c in self.observation.clocks}
            cursors = {c.track_id: c.next_seq for c in plan.cursors}
            for selection in plan.selections:
                # A bounded independent decoder per track avoids assuming
                # cross-stream demux/DTS order equals presentation order.
                container = self._open_container()
                self.containers.append(container)
                stream = next(
                    (s for s in container.streams if s.index == selection.probe_index), None
                )
                if stream is None:
                    _fail("conflict")
                stream.codec_context.thread_count = self.profile.limits.decode.cpu_affinity_count
                track = _Track(
                    stream,
                    selection,
                    clocks[selection.probe_index],
                    next_seq=cursors[selection.track.track_id],
                    decoded=iter(container.decode(stream)),
                )
                self.tracks[selection.probe_index] = track
            self.iterator = True
            self.source_seq = plan.next_source_seq
            self.packet_through = self.source_seq - 1
            # A first physical open/index0 does not prove empty native source
            # history or a zero seek position. Honor the resolved owner's
            # publication cursors and interval; prefix decode stays charged.
            # Native admission, generation and history authority are external.
            if plan.segment.index != 0 and self.profile.protocol.segments != "one_segment_child":
                _fail("conflict")
            # Preserve this child's complete actual probe/hash cost. Segment0
            # native observations of that SAME work cannot count it twice;
            # a later one-segment replacement also carries prior-original
            # spent liability in addition to its own charged reconstruction.
            local_cpu = time.process_time() - self.segment_cpu
            local_wall = time.monotonic() - self.segment_wall
            declared_cpu = plan.budget.cpu_spent_ms / 1000
            declared_wall = plan.budget.wall_spent_ms / 1000
            if plan.segment.index:
                self.cpu_spent = declared_cpu + local_cpu
                self.wall_spent = declared_wall + local_wall
            else:
                self.cpu_spent = max(declared_cpu, local_cpu)
                self.wall_spent = max(declared_wall, local_wall)
            self.segment_cpu, self.segment_wall = time.process_time(), time.monotonic()
        self.plan, self._segment_active = plan, True
        for track in self.tracks.values():
            track.frames_segment = 0
        self._arm()
        return self._next(request)

    def _validate_plan(self, plan):
        formats = {_pin(f) for f in self.profile.formats}
        timed = [
            Fraction(c.first_pts * c.timebase.num, c.timebase.den)
            for c in self.observation.clocks
            if c.provenance != "untimed_image"
        ]
        origin = min(timed) if timed else Fraction(0)
        _fraction(origin)
        for position in timed:
            _q(position - origin)
        probes = {p.index: p for p in self.observation.probe.tracks}
        for selection in plan.selections:
            track, output, probe = selection.track, selection.format, probes[selection.probe_index]
            expected = Fraction(0) if probe.kind == "image" else origin
            if (
                Fraction(track.common_origin.num, track.common_origin.den) != expected
                or _pin(output) not in formats
            ):
                _fail("conflict")
            transform = self.profile.transform
            if probe.kind == "audio":
                rate, channels = probe.rate, probe.channels
                if transform.kind == "audio_resample_downmix":
                    rate, channels = transform.audio_rate, transform.audio_channels
                if (output.rate, output.channels) != (rate, channels):
                    _fail("conflict")
            elif transform.kind == "video_sample_scale" and transform.width is not None:
                self._geometry(transform.width, transform.height)
        if Fraction(plan.segment.end.num, plan.segment.end.den) - Fraction(
            plan.segment.start.num, plan.segment.start.den
        ) > Fraction(self.profile.limits.decode.segment_ms, 1000):
            _fail("bound")

    def _response(self, request, result, *, body=None, **values):
        fields = dict(probe=None, packet=None, end=None, error=None)
        fields.update(values)
        segment_id = None if self.plan is None else self.plan.segment.segment_id
        if result == "probe":
            segment_id = None
        response = m.MediaDecodeResponseV2(
            v=2,
            operation_id=request.operation_id,
            seq=request.seq,
            segment_id=segment_id,
            result=result,
            **fields,
        )
        self._check()
        body = _WireBody(()) if body is None else body
        if response.packet is None:
            if len(body):
                _fail("bad_packet")
        elif (
            len(body) != response.packet.body_bytes
            or body.digest() != response.packet.body_sha256
            or len(body)
            > min(
                m.MAX_MEDIA_BODY_BYTES,
                self.profile.limits.wire.body_bytes,
                self.profile.limits.wire.consumer_bytes,
            )
        ):
            _fail("bound")
        # Canonical public DTO validation remains intact; scatter/gather avoids
        # creating an uncharged header+body duplicate solely for framing.
        header = m.media_bytes(response)
        if len(header) > self.profile.limits.wire.control_bytes:
            _fail("bound")
        return _Response(header, body)

    def _audio(self, track, frame):
        selection, probe = (
            track.selection,
            next(p for p in self.observation.probe.tracks if p.index == track.stream.index),
        )
        start = self._frame_clock(frame, probe)
        if type(frame.samples) is not int or frame.samples <= 0 or frame.sample_rate != probe.rate:
            _fail()
        end = start + Fraction(frame.samples, frame.sample_rate)
        if track.input_end is not None:
            if start < track.input_end:
                _fail()  # Backward/overlapping original audio is unsupported.
            if start > track.input_end:
                yield from self._audio_gap(track)
        track.input_end = end
        track.anchors.append((start, end, frame.pts))
        if len(track.anchors) > self.profile.limits.wire.consumer_packets:
            _fail("bound")
        fmt = selection.format
        layout = "mono" if fmt.channels == 1 else "stereo"
        if (
            fmt.codec == "pcm_s16le"
            and frame.sample_rate == fmt.rate
            and frame.layout.name == layout
            and frame.format.name in ("flt", "fltp")
        ):
            # The existing independent postmux oracle specifies ties-to-even.
            # SIMD float->S16 rounding differs on ARM; canonicalize only an
            # identity format conversion before the unchanged resampler path.
            raw = _float_pcm16(
                list(frame.planes), frame.samples, fmt.channels, frame.format.name == "fltp"
            )
            converted = self.av.AudioFrame(format="s16", layout=layout, samples=frame.samples)
            converted.sample_rate, converted.pts, converted.time_base = (
                frame.sample_rate,
                frame.pts,
                frame.time_base,
            )
            plane = converted.planes[0]
            plane.update(raw + bytes(plane.buffer_size - len(raw)))
            frame = converted
        if track.resampler is None:
            track.resampler = self.av.AudioResampler(
                format="s16" if fmt.codec == "pcm_s16le" else "s32",
                layout="mono" if fmt.channels == 1 else "stereo",
                rate=fmt.rate,
            )
        for output in track.resampler.resample(frame):
            yield from self._pcm(track, output)

    def _audio_gap(self, track):
        # Flush the OLD interval before adding a new anchor or holding the
        # future frame at a segment boundary. Buffered old output must neither
        # borrow the future frame's clock nor be stranded behind that boundary.
        if track.resampler is not None:
            for output in track.resampler.resample(None):
                if output is not None:
                    yield from self._pcm(track, output)
            track.resampler = None
        track.anchors.clear()
        track.output_end = None

    def _pcm(self, track, frame):
        fmt = track.selection.format
        if frame.pts is None or frame.sample_rate != fmt.rate or frame.samples <= 0:
            _fail()
        if sys.byteorder != "little":
            _fail("unavailable")
        start = Fraction(frame.pts) * _fraction(frame.time_base)
        if track.output_end is not None and start != track.output_end:
            _fail()
        track.output_end = start + Fraction(frame.samples, fmt.rate)
        native_width = 2 if fmt.codec == "pcm_s16le" else 4
        size = frame.samples * fmt.channels * native_width
        plane = memoryview(frame.planes[0])
        if len(plane) < size or len(frame.planes) != 1:
            _fail()
        width = 3 if fmt.codec == "pcm_s24le" else native_width
        origin = Fraction(
            track.selection.track.common_origin.num, track.selection.track.common_origin.den
        )
        maximum = min(
            384000,
            self.profile.limits.wire.body_bytes,
            self.profile.limits.wire.consumer_bytes,
            self.profile.limits.wire.total_pending_bytes,
        )
        header = 44 if fmt.container == "wav" else 0
        samples_limit = min(
            fmt.rate,
            fmt.rate * self.profile.limits.wire.audio_packet_ms // 1000,
            (maximum - header) // (width * fmt.channels),
        )
        if samples_limit < 1:
            _fail("bound")
        offset = 0
        while offset < frame.samples:
            position = start + Fraction(offset, fmt.rate)
            while track.anchors and track.anchors[0][1] <= position:
                track.anchors.popleft()
            if not track.anchors or not track.anchors[0][0] <= position < track.anchors[0][1]:
                _fail()
            anchor_start, anchor_end, pts = track.anchors[0]
            available = (anchor_end - position) * fmt.rate
            # A sample crossing an anchor boundary retains its first original
            # anchor; its whole selected PCM sample is never silently dropped.
            count = min(samples_limit, frame.samples - offset, max(1, math.ceil(available)))
            source_pts = m.MediaSourcePTS(
                pts=pts,
                time_base_num=track.clock.timebase.num,
                time_base_den=track.clock.timebase.den,
                sample_start=track.sample,
                sample_count=count,
                frame_index=None,
            )
            piece_start = position - origin
            piece_end = piece_start + Fraction(count, fmt.rate)
            # Keep original resampler scratch until this head is selected.
            # Splits below share it instead of copying a WAV/raw body twice.
            payload = _PCM(plane.toreadonly(), offset, count, native_width, width, fmt)
            yield _Piece(track, piece_start, piece_end, source_pts, payload, width)
            track.sample += count
            offset += count

    def _split_audio(self, piece, boundary):
        """Own sample STARTs before boundary, retaining the exact grid suffix."""
        fmt = piece.track.selection.format
        count = max(0, min(piece.pts.sample_count, math.ceil((boundary - piece.start) * fmt.rate)))
        if count == 0:
            return None, piece
        if count == piece.pts.sample_count:
            return piece, None
        grid = piece.start + Fraction(count, fmt.rate)
        left_payload, right_payload = piece.payload.split(count)
        left_pts = m.MediaSourcePTS(**{**piece.pts.__dict__, "sample_count": count})
        right_pts = m.MediaSourcePTS(
            **{
                **piece.pts.__dict__,
                "sample_start": piece.pts.sample_start + count,
                "sample_count": piece.pts.sample_count - count,
            }
        )
        left = _Piece(
            piece.track,
            piece.start,
            grid,
            left_pts,
            left_payload,
            piece.sample_width,
        )
        right = _Piece(
            piece.track,
            grid,
            piece.end,
            right_pts,
            right_payload,
            piece.sample_width,
        )
        return left, right

    def _image(self, track, frame):
        probe = next(p for p in self.observation.probe.tracks if p.index == track.stream.index)
        position = self._frame_clock(frame, probe)
        original_pts = 0 if probe.kind == "image" else frame.pts
        self._geometry(frame.width, frame.height)
        if probe.kind == "image" and track.frame != 0:
            _fail()
        index = track.frame
        track.frame += 1
        transform, fmt = self.profile.transform, track.selection.format
        rebased = position - Fraction(
            track.selection.track.common_origin.num, track.selection.track.common_origin.den
        )
        if rebased >= Fraction(self.plan.segment.start.num, self.plan.segment.start.den):
            track.frames_segment += 1
            if track.frames_segment > self.profile.limits.decode.frames_per_track_segment:
                _fail("bound")
        while track.times and position - track.times[0] >= 1:
            track.times.popleft()
        track.times.append(position)
        if len(track.times) > math.ceil(
            Fraction(
                self.profile.limits.decode.frame_rate.num, self.profile.limits.decode.frame_rate.den
            )
        ):
            _fail("bound")
        if (
            transform.kind == "video_sample_scale"
            and transform.video_rate is not None
            and probe.kind == "video"
        ):
            period = Fraction(transform.video_rate.den, transform.video_rate.num)
            if track.next_video_time is not None and position < track.next_video_time:
                return
            track.next_video_time = position + period
        width, height = frame.width, frame.height
        if transform.kind == "video_sample_scale" and transform.width is not None:
            width, height = transform.width, transform.height
        self._geometry(width, height)
        origin = Fraction(
            track.selection.track.common_origin.num, track.selection.track.common_origin.den
        )
        start = position - origin
        # Elementary still/frame representations are observations at the exact
        # original presentation instant. They do not invent a duration from
        # frame rate or stretch a point across a native segment boundary.
        end = start
        pts = m.MediaSourcePTS(
            pts=original_pts,
            time_base_num=track.clock.timebase.num,
            time_base_den=track.clock.timebase.den,
            sample_start=None,
            sample_count=None,
            frame_index=index,
        )
        yield _Piece(track, start, end, pts, _Image(frame, width, height, fmt))

    def _before_end(self, position, point=False):
        """One consistent half-open/zero-width-point membership rule."""
        segment = self.plan.segment
        start = Fraction(segment.start.num, segment.start.den)
        end = Fraction(segment.end.num, segment.end.den)
        return position < end or (point and start == end and position == end)

    def _track_pieces(self, track):
        probe = next(p for p in self.observation.probe.tracks if p.index == track.stream.index)
        origin = Fraction(
            track.selection.track.common_origin.num, track.selection.track.common_origin.den
        )
        for frame in track.decoded:
            self._check()
            position = self._frame_clock(frame, probe)
            if not track.first_seen:
                if probe.kind != "image" and frame.pts != track.clock.first_pts:
                    _fail("conflict")
                track.first_seen = True
            position -= origin
            if position < 0:
                _fail("conflict")
            point = probe.kind != "audio"
            if not point and track.input_end is not None and position + origin > track.input_end:
                yield from self._audio_gap(track)
            while not self._before_end(position, point):
                yield _Boundary(position, point)
            if track.selection.track.kind == "audio":
                yield from self._audio(track, frame)
            else:
                yield from self._image(track, frame)
        if track.resampler is not None:
            for frame in track.resampler.resample(None):
                # PyAV17's passthrough resampler returns [None] on flush;
                # this is not an output frame or a manufactured timestamp.
                if frame is not None:
                    yield from self._pcm(track, frame)
        track.eof = True

    def _decoded(self):
        generators = {i: self._track_pieces(t) for i, t in self.tracks.items()}
        candidates = {i: next(g, None) for i, g in generators.items()}
        while True:
            self._check()
            end = Fraction(self.plan.segment.end.num, self.plan.segment.end.den)
            for i, value in list(candidates.items()):
                if isinstance(value, _Boundary) and self._before_end(value.start, value.point):
                    candidates[i] = next(generators[i], None)
            if all(value is None for value in candidates.values()):
                self.eof = True
                return
            available = [
                (value.start, i, value)
                for i, value in candidates.items()
                if isinstance(value, _Piece)
                and self._before_end(value.start, not value.sample_width)
            ]
            if not available:
                yield _Boundary(end)
                continue
            _, index, piece = min(available, key=lambda item: (item[0], item[1]))
            if piece.sample_width and piece.end > end:
                left, right = self._split_audio(piece, end)
                if right is None:
                    yield left
                    candidates[index] = next(generators[index], None)
                else:
                    candidates[index] = right
                    if left is not None:
                        yield left
                # Keep the right half blocked while other original tracks
                # finish this segment; do not end the whole source early.
                continue
            yield piece
            candidates[index] = next(generators[index], None)

    def _end(self, request):
        segment = self.plan.segment
        self._segment_active = False
        tracks = [
            m.MediaTrackEnd(
                track_id=t.selection.track.track_id,
                through_track_seq=t.next_seq - 1,
                through_source_seq=t.through_source or self.packet_through,
                last_pts=t.last_pts,
                state="eof" if t.eof else "segment_boundary",
            )
            for t in self.tracks.values()
        ]
        end = m.MediaDecodeEnd(
            v=1,
            segment_id=segment.segment_id,
            source=request.source,
            sealed_source=request.seal,
            child_owner=request.child_owner,
            profile_sha256=request.profile_sha256,
            kind="source_eof" if self.eof else "segment_end",
            through_packet_sequence=self.packet_through,
            tracks=tracks,
        )
        return self._response(request, "end", end=end)

    def _next(self, request):
        if self.plan is None or not self._segment_active:
            _fail("conflict")
        if self.pieces is None:
            self.pieces = self._decoded()
        start = Fraction(self.plan.segment.start.num, self.plan.segment.start.den)
        end = Fraction(self.plan.segment.end.num, self.plan.segment.end.den)
        while True:
            self._check()
            piece = self.pending
            if piece is None:
                piece = next(self.pieces, None)
            self.pending = None
            if piece is None:
                return self._end(request)
            if isinstance(piece, _Boundary):
                if not self._before_end(piece.start, piece.point):
                    self.pending = piece
                    return self._end(request)
                continue
            if (
                piece.end < piece.start
                or piece.end * 1000 > self.profile.limits.source.file_duration_ms
            ):
                _fail("bound")
            if not self._before_end(piece.start, not piece.sample_width):
                self.pending = piece
                return self._end(request)
            if piece.end <= start and (piece.start != piece.end or piece.start < start):
                continue
            if piece.start < start:
                if not piece.sample_width:
                    _fail()
                _, piece = self._split_audio(piece, start)
                if piece is None:
                    continue
            if piece.end > end:
                # Logical selection owns whole sample STARTs. Its final cell
                # can extend strictly less than one output sample past end.
                if (
                    not piece.sample_width
                    or piece.end - end >= Fraction(1, piece.track.selection.format.rate)
                    or piece.end - Fraction(1, piece.track.selection.format.rate) >= end
                ):
                    _fail()
            track = piece.track
            if self.source_seq > m.MAX_MEDIA_INTEGER or track.next_seq > m.MAX_MEDIA_INTEGER:
                _fail("bound")
            body = piece.payload.materialize(self)
            packet = m.MediaPacketHeader(
                v=1,
                operation_id=request.operation_id,
                consumer=request.consumer,
                source=request.source,
                fence=request.fence,
                payload="video_frame"
                if track.selection.track.kind == "video"
                else track.selection.track.kind,
                track_id=track.selection.track.track_id,
                source_seq=self.source_seq,
                through_source_seq=self.source_seq,
                track_seq=track.next_seq,
                pts=piece.pts,
                range=None,
                start=_q(piece.start),
                end=_q(piece.end),
                format_sha256=track.selection.track.format_sha256,
                body_bytes=len(body),
                body_sha256=body.digest(),
                continuity_id=self.plan.segment.continuity_id,
                sequence=self.source_seq,
            )
            track.next_seq += 1
            track.through_source = self.packet_through = self.source_seq
            track.last_pts = piece.pts
            self.source_seq += 1
            return self._response(request, "packet", packet=packet, body=body)

    def _read_exact(self, count):
        parts = bytearray()
        while len(parts) < count:
            self._check()
            value = self.channel.recv(count - len(parts))
            if not value:
                _fail("bad_packet")
            parts.extend(value)
        return bytes(parts)

    def run(self):
        while True:
            prefix = self._read_exact(4)
            size = struct.unpack(">I", prefix)[0]
            if not 0 < size <= m.MAX_MEDIA_CONTROL_BYTES:
                _fail("bound")
            raw = prefix + self._read_exact(size)
            request = decoder_request_v2_json(raw)
            profile = self.profile or request.profile
            if profile is not None and size > profile.limits.wire.control_bytes:
                _fail("bound")
            if self.last_request == raw:
                self._arm()
                self.last_response.send(self.channel)
                continue
            if request.seq != self.request_seq + 1:
                _fail("conflict")
            identity = tuple(
                _pin(getattr(request, name)) if name != "profile_sha256" else request.profile_sha256
                for name in ("source", "seal", "consumer", "fence", "child_owner", "profile_sha256")
            ) + (request.operation_id,)
            if self.request_identity is None:
                if request.command != "probe":
                    _fail("conflict")
                self.request_identity = identity
            elif identity != self.request_identity:
                _fail("conflict")
            if self.fenced:
                _fail("stale")
            # Only an exact next-sequence, same-original, fully decoded request
            # retires replay. Drop both aliases before any new wire allocation;
            # malformed/foreign/replay requests above retain the original.
            response = self.last_response = None
            try:
                if request.command == "probe":
                    response = self._probe(request)
                elif request.command == "open":
                    response = self._open(request)
                elif request.command == "next":
                    response = self._next(request)
                else:
                    self.fenced = True
                    self.close()
                    response = self._response(request, "cancel_ack")
            except Exception as exc:
                self.fenced = True
                code = (
                    exc.code
                    if isinstance(exc, (DecoderError, m.MediaContractError))
                    else "unavailable"
                )
                # Only a fully parsed, original-bound request can receive this
                # fixed error frame. Framing/identity failures exit with EOF.
                error = m.MediaDecodeResponseV2(
                    v=2,
                    operation_id=request.operation_id,
                    seq=request.seq,
                    segment_id=None if self.plan is None else self.plan.segment.segment_id,
                    result="error",
                    probe=None,
                    packet=None,
                    end=None,
                    error=m.MediaError(v=1, error=code),
                )
                self.channel.sendall(decoder_response_v2_bytes(error, b""))
                raise DecoderError(code) from None
            self._check()
            self.request_seq = request.seq
            self.last_request, self.last_response = raw, response
            response.send(self.channel)
            if self.fenced:
                return

    def close(self):
        while self.containers:
            container = self.containers.pop()
            container.close()


def main():
    """No argv paths/configuration, stdout protocol or descriptor replacement."""
    decoder = channel = None
    try:
        if len(sys.argv) != 1:
            _fail("bad_packet")
        channel = socket.socket(fileno=3)
        if (
            channel.family != socket.AF_UNIX
            or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
        ):
            _fail("bad_packet")
        channel.getpeername()
        # Alarm expiry terminates the original child; it is not a cleanup ACK.
        signal.signal(signal.SIGALRM, lambda *_: os._exit(2))
        decoder = Decoder(channel)
        decoder.run()
        return 0
    except BaseException:
        return 2
    finally:
        if decoder is not None:
            try:
                decoder.close()
            except BaseException:
                pass  # OS exit/reap remains the parent's actual close proof.
        if channel is not None:
            try:
                channel.close()
            except BaseException:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
