"""Trusted bounded worker, executed only in the native private PID/net sandbox."""

import hashlib
import json
import os
import re
import resource
import struct
import subprocess
import sys
import zlib
from pathlib import Path


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(width, height, rgba):
    rows = b"".join(b"\x00" + rgba[y * width * 4 : (y + 1) * width * 4] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 1))
        + chunk(b"IEND", b"")
    )


def run():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (210, 215))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    sys.path.insert(0, "/parser")
    from meta_sam_parser import (
        CompletedOutcome,
        SegmentationMaskRecord,
        decode_mask_to_raster,
        video_segmentation_format,
    )

    profile = json.loads(Path("profile.json").read_text())
    source = Path("source.mp4").read_bytes()
    if hashlib.sha256(source).hexdigest() != profile["source_sha256"]:
        raise ValueError("Source digest changed")
    probe = subprocess.run(
        [
            "/usr/bin/ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=width,height,r_frame_rate,nb_read_frames:format=duration",
            "-of",
            "json",
            "source.mp4",
        ],
        check=True,
        capture_output=True,
        timeout=25,
    )
    if len(probe.stdout) > 16384:
        raise ValueError("Source probe exceeds bound")
    actual = json.loads(probe.stdout)
    stream = actual["streams"][0]
    if (
        stream["width"] != profile["width"]
        or stream["height"] != profile["height"]
        or int(stream["nb_read_frames"]) != profile["frame_count"]
        or stream["r_frame_rate"] != f"{profile['fps_num']}/{profile['fps_den']}"
        or abs(float(actual["format"]["duration"]) - profile["duration_seconds"]) > 0.01
    ):
        raise ValueError("Source video differs from independently measured profile")
    text = Path("sam.txt").read_text()
    if not 0 < len(text.encode()) <= 48 * 1024 * 1024:
        raise ValueError("SAM text exceeds bound")
    lines = text.splitlines()
    if not 1 <= len(lines) <= 1800 or any(len(line) > 512 * 1024 for line in lines):
        raise ValueError("SAM lines exceed bound")
    # The official codec deliberately has no allocation quota. Fence all mask
    # dimensions BEFORE invoking it, including malformed/repeated field tokens.
    whitespace = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
    areas, coordinates = [], set()
    for token in re.finditer(r"<\|([A-Za-z][A-Za-z0-9_.-]*)(?:;([^|]*))?\|>", text):
        if token[1] not in {"mask", "box"}:
            raise ValueError("Unsupported SAM token")
        fields = {}
        for part in (token[2] or "").split(";"):
            part = part.strip(whitespace)
            if not part:
                continue
            key, separator, value = part.partition("=")
            key, value = key.strip(whitespace), value.strip(whitespace)
            if not separator or key in fields:
                raise ValueError("Repeated or malformed SAM field")
            fields[key] = value
        if token[1] == "mask":
            if not {"x", "y", "data"} <= fields.keys() or set(fields) - {"x", "y", "data", "c"}:
                raise ValueError("Unexpected SAM mask fields")
            dimensions = re.fullmatch(r"([0-9]{1,7}),([0-9]{1,7}),([!~][^|]+)", fields["data"])
            if dimensions is None:
                raise ValueError("Malformed SAM mask dimensions")
            areas.append(int(dimensions[1]) * int(dimensions[2]))
        else:
            if not {"x1", "y1", "x2", "y2", "w", "h"} <= fields.keys() or set(fields) - {
                "x1",
                "y1",
                "x2",
                "y2",
                "w",
                "h",
                "c",
            }:
                raise ValueError("Unexpected SAM box fields")
            if any(re.fullmatch(r"[0-9]{1,7}", fields[key]) is None for key in ("w", "h")):
                raise ValueError("Invalid SAM coordinate dimensions")
            coordinates.add((int(fields["w"]), int(fields["h"])))
    if not 1 <= len(areas) <= 8000:
        raise ValueError("SAM mask count exceeds bound")
    if any(not 1 <= area <= 262144 for area in areas) or sum(areas) > 64 * 1024 * 1024:
        raise ValueError("SAM decoded raster quota exceeded")
    if len(coordinates) != 1:
        raise ValueError("Consistent explicit SAM source coordinate size required")
    cw, ch = next(iter(coordinates))
    width, height = profile["width"], profile["height"]
    if not 1 <= cw <= width or not 1 <= ch <= height or cw * height != ch * width:
        raise ValueError("SAM source coordinate aspect differs")
    parser = video_segmentation_format().create_parser()
    parser.push(text, emit=False)
    result = parser.finish(CompletedOutcome()).result
    if result.diagnostics or any(record.kind == "text" for record in result.records):
        raise ValueError("SAM output is malformed or has unsupported fields")
    latest = {}
    for record in result.records:
        if isinstance(record, SegmentationMaskRecord):
            frame = record.frame.frame_index
            if not 0 <= frame < profile["frame_count"]:
                raise ValueError("SAM frame outside source video")
            latest[(frame, record.object_id)] = record
    if not latest:
        raise ValueError("No validated SAM masks")
    by_frame, pixels, overlay = {}, 0, hashlib.sha256()
    for (frame, object_id), record in sorted(latest.items()):
        bounds = record.bounds
        left, top = round(bounds.left * width / cw), round(bounds.top * height / ch)
        right, bottom = round(bounds.right * width / cw), round(bounds.bottom * height / ch)
        if not 0 <= left < right <= width or not 0 <= top < bottom <= height:
            raise ValueError("SAM mask bounds outside source")
        pixels += (right - left) * (bottom - top)
        if pixels > 256 * 1024 * 1024:
            raise ValueError("Projected SAM overlay exceeds pixel quota")
        raster = decode_mask_to_raster(record.mask)
        overlay.update(
            json.dumps(
                [frame, object_id, left, top, right, bottom, record.mask.width, record.mask.height],
                separators=(",", ":"),
            ).encode()
        )
        overlay.update(raster)
        by_frame.setdefault(frame, []).append((left, top, right, bottom, record.mask, raster))
    blank = bytes(width * height * 4)
    blank_png = png(width, height, blank)
    for frame in range(profile["frame_count"]):
        rows = by_frame.get(frame, [])
        if not rows:
            encoded = blank_png
        else:
            rgba = bytearray(blank)
            for left, top, right, bottom, mask, raster in rows:
                for y in range(top, bottom):
                    sy = min(mask.height - 1, (y - top) * mask.height // (bottom - top))
                    for x in range(left, right):
                        sx = min(mask.width - 1, (x - left) * mask.width // (right - left))
                        if raster[sy * mask.width + sx]:
                            offset = (y * width + x) * 4
                            rgba[offset : offset + 4] = b"\x23\xff\x20\xc0"
            encoded = png(width, height, rgba)
        Path(f"mask-{frame:06d}.png").write_bytes(encoded)
    subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-threads",
            "2",
            "-i",
            "source.mp4",
            "-framerate",
            f"{profile['fps_num']}/{profile['fps_den']}",
            "-i",
            "mask-%06d.png",
            "-filter_complex_threads",
            "1",
            "-filter_complex",
            "[0:v][1:v]overlay=shortest=1[v]",
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-preset",
            "veryfast",
            "-crf",
            "25",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            "highlighted.mp4",
        ],
        check=True,
        timeout=160,
    )
    rendered = Path("highlighted.mp4").read_bytes()
    if not 16 <= len(rendered) <= 32 * 1024 * 1024:
        raise ValueError("Rendered MP4 exceeds bound")
    Path("receipt.json").write_text(
        json.dumps(
            {
                "schema_version": "sam_video_render.v1",
                "overlay_sha256": overlay.hexdigest(),
                "rendered_sha256": hashlib.sha256(rendered).hexdigest(),
                "source_sha256": profile["source_sha256"],
                "mask_count": len(latest),
                "frames_with_masks": len(by_frame),
                "source_frame_count": profile["frame_count"],
                "parser_commit": "9348a8a791d3e5b6417232227e32e10464cdf49f",
            },
            allow_nan=False,
        )
        + "\n"
    )


if __name__ == "__main__":
    run()
