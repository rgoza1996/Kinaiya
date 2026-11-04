"""Opt-in exact-byte video admission for the existing run-local budget gateway.

The owner supplies a private manifest, independently measured from the original
source. Neither model requests nor uploaded MP4 metadata create that authority.
No uploads, remote URLs, arbitrary prompts, hosted tools or Wonder are accepted.
"""

import base64
import hashlib
import json
import math
import os
import re
import stat

MAX_VIDEO_BYTES = 16 * 1024 * 1024
MAX_VIDEO_BODY = 23 * 1024 * 1024
VIDEO_LIMIT_USD = 1.50  # Inside, never additional to, the retained combined cap.


def check_json_structure(raw):
    depth, nodes, quoted, escaped = 0, 0, False, False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            nodes += 1
        elif byte in (93, 125):
            depth -= 1
        elif byte in (44, 58):
            nodes += 1
        if depth > 32 or depth < 0 or nodes > 65536:
            raise ValueError("Video JSON structure exceeds its bound")
    if quoted or depth:
        raise ValueError("Incomplete video JSON")


def finite_float(raw):
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("Nonfinite video JSON")
    return value


def load_video_grants(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 16384
        ):
            raise ValueError("Private bounded native video manifest required")
        raw = os.read(fd, 16385)
        after = os.fstat(fd)
        if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("Video manifest changed")
    finally:
        os.close(fd)

    def pairs(items):
        value = {}
        for key, child in items:
            if key in value:
                raise ValueError("Duplicate native video authority field")
            value[key] = child
        return value

    check_json_structure(raw)
    value = json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_float=finite_float,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite authority JSON")),
    )
    if type(value) is not list or not 1 <= len(value) <= 8:
        raise ValueError("One to eight native video grants required")
    grants = {}
    for row in value:
        if type(row) is not dict or set(row) != {"sha256", "bytes", "frames", "prompts"}:
            raise ValueError("Invalid native video grant")
        sha = row["sha256"]
        if type(sha) is not str or not re.fullmatch(r"[0-9a-f]{64}", sha) or sha in grants:
            raise ValueError("Unique source digest required")
        if type(row["bytes"]) is not int or not 16 <= row["bytes"] <= MAX_VIDEO_BYTES:
            raise ValueError("Video byte bound exceeded")
        if type(row["frames"]) is not int or not 1 <= row["frames"] <= 1800:
            raise ValueError("Video frame bound exceeded")
        prompts = row["prompts"]
        if type(prompts) is not dict or set(prompts) != {"sam-3.1", "muse-spark-1.3-contributor"}:
            raise ValueError("Exact video targets required")
        if any(
            type(x) is not str or not re.fullmatch(r"[0-9a-f]{64}", x) for x in prompts.values()
        ):
            raise ValueError("Native prompt digests required")
        grants[sha] = row
    return grants


def contains_video(value):
    # Inspect semantic content parts, not JSON embedded in ordinary text/tool
    # history. This adds no new node limit to long, already-budgeted text tasks.
    rows = value.get("input") if type(value) is dict else None
    if type(rows) is not list:
        return False
    return any(
        type(row) is dict
        and type(row.get("content")) is list
        and any(type(part) is dict and part.get("type") == "input_video" for part in row["content"])
        for row in rows
    )


def video_request(data, grants):
    if type(data) is not dict:
        raise ValueError("Exact video object required")
    model = data.get("model") if type(data) is dict else None
    keys = {"model", "input", "stream", "store"}
    if model == "muse-spark-1.3-contributor":
        keys |= {"max_output_tokens", "reasoning", "tools"}
    elif model != "sam-3.1":
        raise ValueError("Video model is not authorized")
    if set(data) != keys or data["stream"] is not False or data["store"] is not False:
        raise ValueError("Exact buffered stateless video request required")
    if model != "sam-3.1" and (
        data["tools"] != []
        or data["reasoning"] != {"effort": "xhigh"}
        or type(data["max_output_tokens"]) is not int
        or not 1 <= data["max_output_tokens"] <= 8192
    ):
        raise ValueError("Bounded xhigh video understanding required")
    rows = data["input"]
    if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
        raise ValueError("One video message required")
    row = rows[0]
    if set(row) != {"type", "role", "content"} or row["type"] != "message" or row["role"] != "user":
        raise ValueError("Invalid video message")
    parts = row["content"]
    if type(parts) is not list or len(parts) != 2 or any(type(p) is not dict for p in parts):
        raise ValueError("One exact prompt and video required")
    text, video = parts
    if (
        set(text) != {"type", "text"}
        or text["type"] != "input_text"
        or type(text["text"]) is not str
        or not 1 <= len(text["text"].encode("utf-8")) <= 16000
        or set(video) != {"type", "video_url"}
        or video["type"] != "input_video"
        or type(video["video_url"]) is not str
        or not video["video_url"].startswith("data:video/mp4;base64,")
    ):
        raise ValueError("Inline original MP4 required")
    raw = base64.b64decode(video["video_url"].split(",", 1)[1], validate=True)
    sha = hashlib.sha256(raw).hexdigest()
    grant = grants.get(sha)
    if grant is None or len(raw) != grant["bytes"] or raw[4:8] != b"ftyp":
        raise ValueError("Video source is not authorized")
    if hashlib.sha256(text["text"].encode("utf-8")).hexdigest() != grant["prompts"][model]:
        raise ValueError("Video prompt is not authorized")
    return grant
