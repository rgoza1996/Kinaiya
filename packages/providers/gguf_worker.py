"""Offline native GGUF child boundary: sealed inputs, fixed bwrap exec.

The parent touches only the small manifest and file metadata. This child
seals every listed asset, builds a fixed bubblewrap invocation with
per-file guest symlinks to inherited sealed FDs, records a bounded phase receipt outside
the mounted IPC directory, then ``execve``s the sandbox. No subprocess,
no fallback, no network, no credentials, no GPU grant.
"""

import argparse
import fcntl
import json
import os
import resource
import shlex
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path

PHASE_VERSION = 1
PHASE_NAMES = ("preparing", "exec_pending", "pre_exec_failed")
MODEL_GUEST = "/model.gguf"
SERVER_GUEST = "/runtime/llama-server"

_CTX_MIN, _CTX_MAX = 64, 32768
_NPRED_MIN, _NPRED_MAX = 16, 2048
_MEM_MIN, _MEM_MAX = 8 * 1024**3, 32 * 1024**3


def _validate_counts(ctx_size, max_new_tokens, cpu_count):
    if type(ctx_size) is not int or not _CTX_MIN <= ctx_size <= _CTX_MAX:
        raise ValueError("Invalid GGUF ctx size")
    if type(max_new_tokens) is not int or not _NPRED_MIN <= max_new_tokens <= _NPRED_MAX:
        raise ValueError("Invalid GGUF n-predict")
    if max_new_tokens > ctx_size:
        raise ValueError("Invalid GGUF n-predict")
    if type(cpu_count) is not int or cpu_count != 2:
        raise ValueError("GGUF worker requires exactly two CPUs")
    return ctx_size, max_new_tokens


def _native_argv(manifest, ctx_size, max_new_tokens):
    alias = manifest["alias"]
    sampling = manifest["sampling"]
    return [
        "/runtime/llama-server",
        "--model",
        "/model.gguf",
        "--alias",
        str(alias),
        "--host",
        "/workspace/model.sock",
        "--offline",
        "--no-mmproj",
        "--no-warmup",
        "--fit",
        "off",
        "--no-context-shift",
        "--parallel",
        "1",
        "--ctx-size",
        str(ctx_size),
        "--n-predict",
        str(max_new_tokens),
        "--threads",
        "2",
        "--threads-batch",
        "2",
        "--batch-size",
        "256",
        "--ubatch-size",
        "128",
        "--gpu-layers",
        "0",
        "--device",
        "none",
        "--temp",
        str(sampling["temperature"]),
        "--top-p",
        str(sampling["top_p"]),
        "--top-k",
        str(sampling["top_k"]),
        "--min-p",
        str(sampling["min_p"]),
        "--presence-penalty",
        str(sampling["presence_penalty"]),
        "--repeat-penalty",
        str(sampling["repeat_penalty"]),
        "--seed",
        "0",
        "--chat-template-kwargs",
        '{"enable_thinking":false}',
        "--log-disable",
    ]


def build_args(manifest, sealed, socket_dir, *, ctx_size=2048, max_new_tokens=128, cpu_count=2):
    """Build fixed bwrap argv; guest links resolve only inherited sealed inputs."""
    from core.policy import ToolPolicy
    from services.agent.tools import build_bwrap_args

    _validate_counts(ctx_size, max_new_tokens, cpu_count)
    from packages.providers.gguf_assets import validate_manifest

    validate_manifest(manifest)
    guests = {entry["guest"] for entry in manifest["assets"]}
    if not isinstance(sealed, dict) or set(sealed) != guests:
        raise ValueError("Sealed inputs must match manifest guests exactly")
    required = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
    for entry in manifest["assets"]:
        fd = sealed[entry["guest"]]
        if type(fd) is not int or fd < 0:
            raise ValueError("Invalid sealed descriptor")
        try:
            info = os.fstat(fd)
            seals = fcntl.fcntl(fd, fcntl.F_GET_SEALS)
        except (OSError, OverflowError) as exc:
            raise ValueError("GGUF inputs must be sealed descriptors") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size != entry["bytes"]
            or seals & required != required
        ):
            raise ValueError("GGUF inputs must be sealed descriptors with pinned sizes")
    socket_dir, _ = _private_dir(socket_dir, "socket dir")
    ipc, _ = _private_dir(socket_dir / "ipc", "ipc dir")
    policy = ToolPolicy(workspace=ipc)
    command = "exec " + shlex.join(_native_argv(manifest, ctx_size, max_new_tokens))
    argv, isolated = build_bwrap_args(policy, command, False)
    if isolated is not True:
        raise ValueError("GGUF worker requires network isolation")
    try:
        bash_at = len(argv) - 3
        if argv[bash_at : bash_at + 2] != ["/bin/bash", "-c"] or argv[bash_at + 2] != command:
            raise ValueError("Unexpected sandbox command shape")
    except IndexError as exc:
        raise ValueError("Unexpected sandbox command shape") from exc
    needs_runtime = any(str(g).startswith("/runtime/") for g in guests)
    needs_vendor = any(str(g).startswith("/vendor/") for g in guests)
    extra = []
    if needs_runtime:
        extra += ["--dir", "/runtime"]
    if needs_vendor:
        extra += ["--dir", "/vendor"]
    for guest in sorted(guests):
        # bwrap realpath()s --ro-bind-fd sources, which cannot resolve anonymous
        # memfds. Unused inherited FDs survive into the payload; exact guest
        # links preserve the original sealed inode without copying its bytes.
        extra += ["--symlink", f"/proc/self/fd/{sealed[guest]}", str(guest)]
    extra += [
        "--setenv",
        "LD_LIBRARY_PATH",
        "/runtime:/vendor",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "XDG_CACHE_HOME",
        "/tmp",
        "--setenv",
        "XDG_CONFIG_HOME",
        "/tmp",
        "--setenv",
        "XDG_DATA_HOME",
        "/tmp",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "HF_HUB_OFFLINE",
        "1",
        "--setenv",
        "TRANSFORMERS_OFFLINE",
        "1",
    ]
    return argv[:bash_at] + extra + argv[bash_at:]


def _identity(info):
    return info.st_dev, info.st_ino


def _directory_bound(path, fd):
    current, opened = path.lstat(), os.fstat(fd)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) != 0o700
        or _identity(current) != _identity(opened)
        or path.resolve(strict=True) != path
        or Path(os.readlink(f"/proc/self/fd/{fd}")) != path
    ):
        raise ValueError("Invalid GGUF private directory binding")


@contextmanager
def _open_private(path):
    path = Path(path)
    if not path.is_absolute() or any(c in str(path) for c in "\0\r\n"):
        raise ValueError("Invalid GGUF private directory")
    # O_NOFOLLOW protects the final component; canonical equality also rejects
    # intermediate links and '..'. Always close the descriptor on rejection.
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _directory_bound(path, fd)
        yield fd
    finally:
        os.close(fd)


def _private_dir(path, label):
    with _open_private(path) as fd:
        return Path(path), os.fstat(fd)


def _receipt_binding(info):
    # Reading may legitimately update atime; mutation metadata must stay fixed.
    return (
        _identity(info),
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _receipt_data(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or not 1 <= info.st_size <= 1024
    ):
        raise ValueError("Invalid GGUF phase receipt metadata")
    from packages.providers.gguf_assets import _pairs

    try:
        raw = os.read(fd, 1025)
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (ValueError, RecursionError) as exc:
        raise ValueError("Invalid GGUF phase receipt") from exc
    if (
        len(raw) != info.st_size
        or not isinstance(data, dict)
        or set(data) != {"version", "pid", "phase"}
        or type(data["version"]) is not int
        or data["version"] != PHASE_VERSION
        or type(data["pid"]) is not int
        or data["pid"] != os.getpid()
        or data["phase"] != "preparing"
    ):
        raise ValueError("GGUF phase receipt is not this preparing worker")
    return info


def _write_all(fd, raw):
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("GGUF receipt write failed")
        view = view[written:]
    os.fsync(fd)


def record_phase(socket_dir, phase):
    """Exclusive preparing, then one terminal transition in the same private dir.

    The mounted IPC directory never contains this parent-readable receipt. Any
    failed/uncertain publication must be treated conservatively by the caller.
    """
    if phase not in PHASE_NAMES:
        raise ValueError("Invalid GGUF phase")
    raw = json.dumps(
        {"version": PHASE_VERSION, "pid": os.getpid(), "phase": phase},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path = Path(socket_dir)
    flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    with _open_private(path) as parent:
        if phase == "preparing":
            fd = os.open(
                "phase.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags, 0o600, dir_fd=parent
            )
            try:
                _write_all(fd, raw)
                info = os.fstat(fd)
                if stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                    raise ValueError("Invalid GGUF initial receipt")
                _directory_bound(path, parent)
                if _identity(
                    os.stat("phase.json", dir_fd=parent, follow_symlinks=False)
                ) != _identity(info):
                    raise ValueError("GGUF initial receipt was replaced")
            finally:
                os.close(fd)
        else:
            current = os.open("phase.json", os.O_RDONLY | flags, dir_fd=parent)
            temp = f".phase.{os.getpid()}.tmp"
            created = False
            try:
                prior = _receipt_data(current)
                fd = os.open(
                    temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags, 0o600, dir_fd=parent
                )
                created = True
                fresh = os.fstat(fd)
                try:
                    _write_all(fd, raw)
                    written = os.fstat(fd)
                    if stat.S_IMODE(written.st_mode) != 0o600 or written.st_nlink != 1:
                        raise ValueError("Invalid GGUF temporary receipt")
                    _directory_bound(path, parent)
                    if _receipt_binding(_receipt_data(current)) != _receipt_binding(
                        prior
                    ) or _identity(
                        os.stat("phase.json", dir_fd=parent, follow_symlinks=False)
                    ) != _identity(prior):
                        raise ValueError("GGUF phase receipt was replaced")
                    if _identity(os.stat(temp, dir_fd=parent, follow_symlinks=False)) != _identity(
                        fresh
                    ):
                        raise ValueError("GGUF temporary receipt was replaced")
                    os.replace(temp, "phase.json", src_dir_fd=parent, dst_dir_fd=parent)
                    created = False
                finally:
                    os.close(fd)
            finally:
                os.close(current)
                if created:
                    # Remove only the temporary inode created by this call.
                    try:
                        if _identity(
                            os.stat(temp, dir_fd=parent, follow_symlinks=False)
                        ) == _identity(fresh):
                            os.unlink(temp, dir_fd=parent)
                    except FileNotFoundError:
                        pass
        os.fsync(parent)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid GGUF worker arguments")


def _main(argv):
    from packages.providers.gguf_assets import load_manifest, outside_workspace
    from packages.providers.whistle_assets import sealed_asset

    os.umask(0o077)
    parser = _Parser(allow_abbrev=False)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--socket-dir", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--ctx-size", type=int, default=2048)
    parser.add_argument("--max-new-tokens", type=int, default=128)
    parser.add_argument("--cpu-count", type=int, default=2)
    parser.add_argument("--memory-bytes", type=int, default=16 * 1024**3)
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).resolve()
    _validate_counts(args.ctx_size, args.max_new_tokens, args.cpu_count)
    if not _MEM_MIN <= args.memory_bytes <= _MEM_MAX:
        raise ValueError("GGUF address-space limit must be 8..32 GiB")
    socket_dir, _ = _private_dir(args.socket_dir, "socket dir")
    outside_workspace(socket_dir, workspace)
    with _open_private(socket_dir) as parent:
        try:
            os.mkdir("ipc", 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        with _open_private(socket_dir / "ipc") as ipc:
            if os.listdir(ipc):
                raise ValueError("GGUF IPC directory must be empty")
        _directory_bound(socket_dir, parent)
    record_phase(socket_dir, "preparing")
    pending = False
    try:
        manifest = load_manifest(args.manifest, args.manifest_sha256, workspace)
        with ExitStack() as stack:
            sealed = {
                entry["guest"]: stack.enter_context(sealed_asset(entry["path"], entry, workspace))
                for entry in manifest["assets"]
            }
            argv_exec = build_args(
                manifest,
                sealed,
                socket_dir,
                ctx_size=args.ctx_size,
                max_new_tokens=args.max_new_tokens,
                cpu_count=args.cpu_count,
            )
            for fd in sealed.values():
                os.set_inheritable(fd, True)
            cpus = sorted(os.sched_getaffinity(0))[: args.cpu_count]
            if len(cpus) != args.cpu_count:
                raise ValueError("GGUF requires two available CPUs")
            os.sched_setaffinity(0, cpus)
            resource.setrlimit(resource.RLIMIT_AS, (args.memory_bytes, args.memory_bytes))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            # A failed publication may have replaced the file before fsync failed.
            # Never downgrade that uncertain attempt into no-native-start proof.
            pending = True
            record_phase(socket_dir, "exec_pending")
            resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
            os.execve(argv_exec[0], argv_exec, {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
            raise RuntimeError("GGUF exec unexpectedly returned")
    except BaseException:
        if not pending:
            try:
                record_phase(socket_dir, "pre_exec_failed")
            except Exception:
                pass  # Unconfirmed receipt is uncertainty, never release evidence.
        raise


def main(argv=None):
    try:
        _main(argv)
    except Exception:
        # No owner paths, raw native diagnostics or tracebacks in production logs.
        raise SystemExit("GGUF worker startup failed") from None


if __name__ == "__main__":
    main()
