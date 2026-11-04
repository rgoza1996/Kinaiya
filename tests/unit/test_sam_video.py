"""Synthetic-only protocol/render qualification; no API or physical devices."""

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.agent.sam_video import SamVideoProfile, SamVideoProjector

SAMPLE = (
    "<0f>0<|box;x1=211;y1=228;x2=270;y2=254;w=320;h=334|>"
    "<|mask;x=0;y=0;data=27,60,~!!!!M!0c[0o91w?q1!pIH4pPRVp2B3'7`e.ioeAf6-k/#Xd8%dX9x(|>"
    ",1<|box;x1=155;y1=228;x2=202;y2=254;w=320;h=334|>"
    "<|mask;x=0;y=0;data=27,48,~!!!!J!0c[q=Pj_zs=*4C4(/./x#:/`S_GnD`=o3?X{emCgO$y@|>\n"
)


class Registry:
    def __init__(self):
        self.calls = []

    def register(self, **values):
        self.calls.append(values)
        return {"asset_id": "media_" + "a" * 32, "publication_only": True}


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "synthetic.mp4"
    subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x334:r=1:d=1",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            str(path),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=15,
        env={"PATH": "/usr/bin:/bin"},
    )
    return path.read_bytes()


def setup(tmp_path, clip):
    sha = hashlib.sha256(clip).hexdigest()
    profile = SamVideoProfile(sha, 320, 334, 1, 1, 1, 1.0)
    registry = Registry()
    projector = SamVideoProjector(
        media_assets=registry, work_root=tmp_path / "private", profile=profile
    )
    prepared = SimpleNamespace(
        target=SimpleNamespace(operation="sam_hands"),
        source_bytes=clip,
        grant=SimpleNamespace(
            source_sha256=sha,
            content_scope="private_cloud_authorized",
            binding=SimpleNamespace(
                model_dump=lambda **_: {
                    "task_id": "t",
                    "objective_revision": 1,
                    "session_id": "s",
                    "thread_id": "h",
                    "epoch": 1,
                }
            ),
        ),
    )
    return projector, prepared, registry


@pytest.mark.sandbox
async def test_actual_synthetic_mask_decode_and_isolated_video_render(tmp_path, clip):
    projector, prepared, registry = setup(tmp_path, clip)
    result = await projector(prepared, {}, SAMPLE, lambda: None)
    assert result["data"]["segmentation"]["mask_count"] == 2
    assert result["data"]["segmentation"]["frames_with_masks"] == 1
    assert result["data"]["media_asset"]["publication_only"]
    assert len(registry.calls) == 1
    published = registry.calls[0]
    assert published["source_bytes"] == clip
    assert published["rendered_mp4"][4:8] == b"ftyp"
    assert published["rendered_mp4"] != clip
    assert published["content_scope"] == "private_cloud_authorized"
    rendered_path = tmp_path / "actual-synthetic-highlighted.mp4"
    rendered_path.write_bytes(published["rendered_mp4"])
    decoded = subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(rendered_path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=15,
        env={"PATH": "/usr/bin:/bin"},
    ).stdout
    pixels = list(zip(decoded[0::3], decoded[1::3], decoded[2::3], strict=True))
    assert any(g > r + 30 and g > b + 30 for r, g, b in pixels)
    assert sum(b > r + 80 and b > g + 80 for r, g, b in pixels) > 5000
    assert list(projector.work_root.iterdir()) == []
    assert not projector._owners


@pytest.mark.parametrize(
    "change",
    [
        {"source_sha256": "invalid"},
        {"width": True},
        {"height": 721},
        {"frame_count": 1801},
        {"fps_den": 0},
        {"duration_seconds": float("nan")},
    ],
)
def test_profile_bounds(change):
    value = dict(
        source_sha256="a" * 64,
        width=320,
        height=334,
        fps_num=1,
        fps_den=1,
        frame_count=1,
        duration_seconds=1.0,
    )
    value.update(change)
    with pytest.raises(ValueError):
        SamVideoProfile(**value)


async def test_wrong_source_never_renders_or_registers(tmp_path, clip):
    projector, prepared, registry = setup(tmp_path, clip)
    prepared.source_bytes = clip + b"changed"
    with pytest.raises(ValueError, match="source-bound"):
        await projector(prepared, {}, SAMPLE, lambda: None)
    assert not registry.calls and not projector._owners


@pytest.mark.sandbox
@pytest.mark.parametrize(
    "text",
    [
        SAMPLE.replace("data=27,60", "data=999999,999999"),
        SAMPLE.replace("data=27,60", "\ufeffdata=999999,999999"),
        SAMPLE.replace("w=320;h=334", "h=334; w = 640", 1),
        SAMPLE.replace("<0f>", "<9f>"),
        "No hands found",
    ],
)
async def test_malformed_or_unbounded_masks_never_register(tmp_path, clip, text):
    projector, prepared, registry = setup(tmp_path, clip)
    with pytest.raises(ValueError, match="render refused"):
        await projector(prepared, {}, text, lambda: None)
    assert not registry.calls and not projector._owners


def test_vendored_parser_bytes_match_official_pin():
    root = Path(__file__).resolve().parents[2] / "packages/vendor/meta_sam_parser"
    receipt = json.loads((root / "PROVENANCE.json").read_text())
    assert receipt["commit"] == "9348a8a791d3e5b6417232227e32e10464cdf49f"
    for name, sha in receipt["unchanged_files"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == sha
