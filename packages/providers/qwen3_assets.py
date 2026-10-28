"""Sealed Qwen3 model inputs and runtime identity; importing loads no model runtime.

sealed_model() copies the seven exact manifest files into sealed memfds and
exposes them through a private snapshot directory whose entries symlink to
/proc/self/fd/N. Later owner writes cannot change the sealed bytes consumed
by the native loader. validate_runtime() checks the six pinned runtime core
files plus version pins with stdlib only; it is not full build equivalence.
"""

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

MANIFEST = json.loads(Path(__file__).with_name("qwen3_manifest.json").read_text())
ASSET_NAMES = tuple(MANIFEST["assets"].keys())
RUNTIME = dict(MANIFEST["runtime"])
RUNTIME_CORE = dict(MANIFEST["runtime_core"])
_SNAPSHOT_RE = re.compile(r"model-([1-9][0-9]{0,9})\Z")


def outside_workspace(path, workspace):
    path, workspace = Path(path), Path(workspace).resolve()
    if not path.is_absolute() or any(c in str(path) for c in "\0\r\n"):
        raise ValueError("Qwen3 inputs require absolute owner paths")
    for location in (path, path.resolve(strict=True)):
        if (
            location == workspace
            or location.is_relative_to(workspace)
            or workspace.is_relative_to(location)
        ):
            raise ValueError("Qwen3 inputs must be outside the Agent workspace")
    return path


def _sealed_fd(path, pin, workspace):
    import fcntl

    path = outside_workspace(path, workspace)
    if type(pin.get("bytes")) is not int or not isinstance(pin.get("sha256"), str):
        raise ValueError("Invalid Qwen3 asset pin")
    try:
        source = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise ValueError("Qwen3 asset must be a readable no-follow file") from exc
    target = None
    try:
        info = os.fstat(source)
        if not stat.S_ISREG(info.st_mode) or info.st_size != pin["bytes"]:
            raise ValueError("Qwen3 asset size/type mismatch")
        outside_workspace(os.readlink(f"/proc/self/fd/{source}"), workspace)
        target = os.memfd_create("qwen3-pinned-input", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        digest, count = hashlib.sha256(), 0
        while True:
            chunk = os.read(source, 1024 * 1024)
            if not chunk:
                break
            count += len(chunk)
            if count > pin["bytes"]:
                raise ValueError("Qwen3 asset changed during validation")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(target, view) :]
        if count != pin["bytes"] or digest.hexdigest() != pin["sha256"]:
            raise ValueError("Qwen3 asset digest mismatch")
        fcntl.fcntl(
            target,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL,
        )
        os.lseek(target, 0, os.SEEK_SET)
        fd, target = target, None
        return fd
    finally:
        os.close(source)
        if target is not None:
            os.close(target)


def validate_runtime():
    """Verify six pinned runtime core files plus versions; return runtime dict.

    Uses stdlib metadata/sysconfig only and never imports Torch/Transformers.
    The six-file screen is not full interpreter/package/build equivalence.
    """
    if sys.version_info[:2] != (3, 12):
        raise ValueError("Qwen3 runtime requires Python 3.12")
    try:
        torch_version = importlib.metadata.version("torch")
        transformers_version = importlib.metadata.version("transformers")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ValueError("Qwen3 runtime packages missing") from exc
    if torch_version != RUNTIME["torch"] or transformers_version != RUNTIME["transformers"]:
        raise ValueError("Qwen3 runtime version mismatch")
    roots = {}
    for package in ("torch", "transformers"):
        spec = importlib.util.find_spec(package)
        locations = getattr(spec, "submodule_search_locations", None) if spec else None
        if not locations:
            raise ValueError(f"Qwen3 runtime package {package} not found")
        roots[package] = Path(list(locations)[0])
    for relative, pin in RUNTIME_CORE.items():
        package = relative.split("/", 1)[0]
        if package not in roots:
            raise ValueError("Qwen3 runtime core outside pinned packages")
        full = roots[package] / relative[len(package) + 1 :]
        fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != pin["bytes"]:
                raise ValueError(f"Qwen3 runtime core mismatch: {relative}")
            digest, count = hashlib.sha256(), 0
            while chunk := os.read(fd, 1024 * 1024):
                count += len(chunk)
                if count > pin["bytes"]:
                    raise ValueError(f"Qwen3 runtime core mismatch: {relative}")
                digest.update(chunk)
            if count != pin["bytes"] or digest.hexdigest() != pin["sha256"]:
                raise ValueError(f"Qwen3 runtime core mismatch: {relative}")
        finally:
            os.close(fd)
    return dict(RUNTIME)


@contextmanager
def sealed_model(model_dir, workspace, snapshot_dir):
    """Copy/hash seven manifest files into sealed memfds; yield snapshot path.

    snapshot_dir must be exactly snapshot_path(socket_dir, pid): a new
    owner-private model-PID directory that will contain only the fixed
    manifest filenames symlinked to this process's sealed /proc/self/fd/N.
    All sealed FDs stay open for the whole model lifetime. Cleanup closes
    every FD and removes only the captured owned links/dir.
    """
    from packages.providers.qwen3_ipc import snapshot_path as _snapshot_path

    workspace_resolved = Path(workspace).resolve()
    model_dir = outside_workspace(Path(model_dir), workspace_resolved)
    snapshot_dir = Path(snapshot_dir)
    if not snapshot_dir.is_absolute() or any(c in str(snapshot_dir) for c in "\0\r\n"):
        raise ValueError("Qwen3 snapshot requires an absolute owner path")
    match = _SNAPSHOT_RE.fullmatch(snapshot_dir.name)
    if not match:
        raise ValueError("Qwen3 snapshot name must be model-<pid>")
    pid = int(match.group(1))
    if not 1 <= pid <= 2**31 - 1 or pid != os.getpid():
        raise ValueError("Qwen3 snapshot PID must be this process")
    expected = _snapshot_path(snapshot_dir.parent, pid)
    if Path(expected) != snapshot_dir:
        raise ValueError("Qwen3 snapshot is not the fixed model-PID path")
    parent = outside_workspace(snapshot_dir.parent, workspace_resolved)
    info = parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or os.path.lexists(snapshot_dir)
    ):
        raise ValueError("Qwen3 snapshot parent must be owner-private and snapshot new")
    if len(ASSET_NAMES) != 7:
        raise ValueError("Qwen3 manifest must pin exactly seven model files")
    fds, links = [], []
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    directory_fd, created = None, None
    try:
        if (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("Qwen3 snapshot parent changed")
        for name in ASSET_NAMES:
            pin = MANIFEST["assets"][name]
            fds.append(_sealed_fd(str(Path(model_dir) / name), pin, workspace_resolved))
        os.mkdir(snapshot_dir.name, 0o700, dir_fd=parent_fd)
        directory_fd = os.open(
            snapshot_dir.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        created = os.fstat(directory_fd)
        for name, fd in zip(ASSET_NAMES, fds, strict=True):
            os.symlink(f"/proc/self/fd/{fd}", name, dir_fd=directory_fd)
            link_fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
            links.append((name, link_fd))
        if sorted(os.listdir(directory_fd)) != sorted(ASSET_NAMES):
            raise ValueError("Qwen3 snapshot must contain only fixed manifest filenames")
        yield Path(snapshot_dir)
    finally:
        for name, link_fd in links:
            try:
                owned = os.fstat(link_fd)
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                    os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
            finally:
                os.close(link_fd)
        if created is not None:
            try:
                current = os.stat(snapshot_dir.name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.rmdir(snapshot_dir.name, dir_fd=parent_fd)
            except OSError:
                pass
        if directory_fd is not None:
            os.close(directory_fd)
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        os.close(parent_fd)
