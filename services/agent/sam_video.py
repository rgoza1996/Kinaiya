"""Native source-pinned SAM mask rendering; no model-selected executable code."""

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class SamVideoProfile:
    source_sha256: str
    width: int
    height: int
    fps_num: int
    fps_den: int
    frame_count: int
    duration_seconds: float

    def __post_init__(self):
        if (
            type(self.source_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.source_sha256) is None
            or any(
                type(x) is not int
                for x in (self.width, self.height, self.fps_num, self.fps_den, self.frame_count)
            )
            or not 1 <= self.width <= 1280
            or not 1 <= self.height <= 720
            or not 1 <= self.fps_num <= 120000
            or not 1 <= self.fps_den <= 10000
            or not 1 <= self.fps_num / self.fps_den <= 60
            or not 1 <= self.frame_count <= 1800
            or type(self.duration_seconds) not in (float, int)
            or not math.isfinite(self.duration_seconds)
            or not 0 < self.duration_seconds <= 60
        ):
            raise ValueError("Bounded native source video profile required")


class SamVideoProjector:
    def __init__(self, *, media_assets, work_root, profile):
        if not isinstance(profile, SamVideoProfile) or not callable(
            getattr(media_assets, "register", None)
        ):
            raise ValueError("Native media registry and video profile required")
        self.media_assets, self.profile = media_assets, profile
        self.work_root = Path(work_root)
        if not self.work_root.is_absolute() or self.work_root.resolve() != self.work_root:
            raise ValueError("Protected render root required")
        self.work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.work_root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Private render root required")
        self._owners = set()

    async def __call__(self, prepared, payload, text, current):
        current()
        if (
            prepared.target.operation != "sam_hands"
            or prepared.grant.source_sha256 != self.profile.source_sha256
            or hashlib.sha256(prepared.source_bytes).hexdigest() != self.profile.source_sha256
            or type(text) is not str
            or not 0 < len(text.encode("utf-8")) <= 48 * 1024 * 1024
        ):
            raise ValueError("SAM response differs from the source-bound render profile")
        directory = Path(tempfile.mkdtemp(prefix="sam-", dir=self.work_root))
        for name, content in {
            "source.mp4": prepared.source_bytes,
            "sam.txt": text.encode("utf-8"),
            "profile.json": json.dumps(asdict(self.profile), allow_nan=False).encode(),
        }.items():
            with (directory / name).open("xb") as handle:
                os.chmod(handle.fileno(), 0o600)
                handle.write(content)
        worker = Path(__file__).with_name("_sam_video_worker.py")
        vendor = Path(__file__).resolve().parents[2] / "packages" / "vendor"
        argv = [
            "/usr/bin/bwrap",
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--symlink",
            "usr/bin",
            "/bin",
            "--ro-bind",
            "/etc/alternatives",
            "/etc/alternatives",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--ro-bind",
            str(worker),
            "/worker.py",
            "--ro-bind",
            str(vendor),
            "/parser",
            "--bind",
            str(directory),
            "/work",
            "--chdir",
            "/work",
            "/usr/bin/python3",
            "-I",
            "/worker.py",
        ]
        operation = asyncio.create_task(self._render(argv, directory))
        self._owners.add(operation)
        cancelled = False
        completed = False
        try:
            while True:
                try:
                    receipt, rendered = await asyncio.shield(operation)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    operation.cancel()
                    # Join the same original process owner before propagation.
                    while not operation.done():
                        try:
                            await asyncio.shield(operation)
                        except asyncio.CancelledError:
                            pass
                    raise
            current()
            if cancelled:
                raise asyncio.CancelledError
            result = self.media_assets.register(
                binding=prepared.grant.binding.model_dump(mode="json"),
                content_scope=prepared.grant.content_scope,
                source_bytes=prepared.source_bytes,
                source_sha256=self.profile.source_sha256,
                rendered_mp4=rendered,
                overlay_sha256=receipt["overlay_sha256"],
                width=self.profile.width,
                height=self.profile.height,
                duration_seconds=self.profile.duration_seconds,
                fps_num=self.profile.fps_num,
                fps_den=self.profile.fps_den,
                frame_count=self.profile.frame_count,
            )
            current()
            completed = True
            return {
                "text": f"SAM 3.1 returned validated hand masks on {receipt['frames_with_masks']} of {self.profile.frame_count} source frames. Native highlighted video registered; publication only.",
                "data": {"media_asset": result, "segmentation": receipt},
            }
        finally:
            if operation.done():
                self._owners.discard(operation)
                if completed:
                    shutil.rmtree(directory)

    async def _render(self, argv, directory):
        process = None
        with (directory / "worker.log").open("xb") as log:
            try:
                spawn = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        *argv,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        env={"PATH": "/usr/bin:/bin"},
                    )
                )
                cancelled = False
                while not spawn.done():
                    try:
                        await asyncio.shield(spawn)
                    except asyncio.CancelledError:
                        cancelled = True
                process = spawn.result()
                if cancelled:
                    raise asyncio.CancelledError
                async with asyncio.timeout(240):
                    code = await process.wait()
                if code:
                    raise ValueError("Native SAM render refused; inspect protected worker receipt")
                path = directory / "highlighted.mp4"
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or not 16 <= info.st_size <= 32 * 1024 * 1024:
                    raise ValueError("Rendered video exceeds its bound")
                raw = (directory / "receipt.json").read_bytes()
                if len(raw) > 4096:
                    raise ValueError("Render receipt exceeds its bound")
                receipt = json.loads(raw)
                rendered = path.read_bytes()
                if hashlib.sha256(rendered).hexdigest() != receipt["rendered_sha256"]:
                    raise ValueError("Rendered video changed")
                return receipt, rendered
            finally:
                if process is not None and process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    wait = asyncio.create_task(process.wait())
                    while not wait.done():
                        try:
                            await asyncio.shield(wait)
                        except asyncio.CancelledError:
                            pass
                    wait.result()
