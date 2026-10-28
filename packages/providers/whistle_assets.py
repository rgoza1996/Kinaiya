"""Pinned local Whistle inputs; importing this module never loads native code."""

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

MANIFEST = json.loads(Path(__file__).with_name("whistle_manifest.json").read_text())
PROFILE = MANIFEST["profile"]


def outside_workspace(path, workspace):
    path, workspace = Path(path), Path(workspace).resolve()
    if not path.is_absolute() or any(c in str(path) for c in "\0\r\n"):
        raise ValueError("Whistle inputs require absolute owner paths")
    for location in (path, path.resolve(strict=True)):
        if (
            location == workspace
            or location.is_relative_to(workspace)
            or workspace.is_relative_to(location)
        ):
            raise ValueError("Whistle inputs must be outside the Agent workspace")
    return path


@contextmanager
def sealed_asset(path, pin, workspace):
    """Hash the exact copied bytes and seal them before CDLL/model consumption.

    A later owner write/rename cannot change the validated native inputs. Hashes
    identify pinned artifacts; they do not establish a binary's source provenance.
    """
    import fcntl

    path = outside_workspace(path, workspace)
    source = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    target = None
    try:
        info = os.fstat(source)
        if not stat.S_ISREG(info.st_mode) or info.st_size != pin["bytes"]:
            raise ValueError("Whistle artifact size/type mismatch")
        outside_workspace(os.readlink(f"/proc/self/fd/{source}"), workspace)
        target = os.memfd_create("whistle-pinned-input", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        digest, count = hashlib.sha256(), 0
        while chunk := os.read(source, 1024 * 1024):
            count += len(chunk)
            if count > pin["bytes"]:
                raise ValueError("Whistle artifact changed during validation")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(target, view) :]
        if count != pin["bytes"] or digest.hexdigest() != pin["sha256"]:
            raise ValueError("Whistle artifact digest mismatch")
        fcntl.fcntl(
            target,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL,
        )
        os.lseek(target, 0, os.SEEK_SET)
        yield target
    finally:
        os.close(source)
        if target is not None:
            os.close(target)


def validate_assets(weights, library, workspace):
    for path, name in ((weights, "weights"), (library, "library")):
        with sealed_asset(path, MANIFEST[name], workspace):
            pass
