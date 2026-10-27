"""Trusted runner executed INSIDE the bubblewrap sandbox.

Not importable by candidates: it is bind-mounted read-only at /runner and
runs as PID 1's child with cleared environment. It applies resource limits
in this trusted process *before* loading untrusted code (limits set here
cannot be raised by the candidate), calls ``select_context(payload)``, and
emits exactly one JSON document on the saved stdout fd. Candidate stdout,
stderr, and any extra keys are discarded so forged score/lifecycle fields
can never count as evidence.
"""

from __future__ import annotations

import ctypes
import importlib
import json
import os
import sys
import types


class _LimitsUnavailable(Exception):
    """A required sandbox resource limit could not be applied (fail closed)."""


def _apply_limits(cpu_s: int, mem_mb: int, nproc: int) -> None:
    """Apply required limits; raise :class:`_LimitsUnavailable` on failure.

    CPU (the configured hard limit), address space, file size, and core
    dumps are required: if any cannot apply, the runner refuses to load
    untrusted code. NPROC is intentionally not applied: inside the uid-0
    user namespace the call can leak onto the host user while not reliably
    containing the candidate, so process containment comes from the PID
    namespace plus the host group-kill.
    """
    try:
        import resource
    except ImportError as exc:
        raise _LimitsUnavailable(f"resource module missing: {exc}") from exc
    required = {
        "RLIMIT_CPU": (resource.RLIMIT_CPU, (cpu_s, cpu_s)),
        "RLIMIT_AS": (resource.RLIMIT_AS, (mem_mb * 1024 * 1024,) * 2),
        "RLIMIT_FSIZE": (resource.RLIMIT_FSIZE, (1 << 20, 1 << 20)),
        "RLIMIT_CORE": (resource.RLIMIT_CORE, (0, 0)),
    }
    for name, (key, bounds) in required.items():
        try:
            resource.setrlimit(key, bounds)
        except (ValueError, OSError) as exc:
            raise _LimitsUnavailable(f"cannot apply {name}: {exc}") from exc
    # NPROC is deliberately NOT set here: inside the uid-0 user namespace
    # the call can leak onto the host user's limit (later host forks then
    # fail with EAGAIN) while not reliably containing the candidate, so
    # descendant containment comes from the PID namespace plus the host
    # group-kill instead. ``nproc`` is accepted for API compatibility only.
    _ = nproc


def _clean_sys_path(source_dir: str) -> None:
    keep = [
        p
        for p in sys.path
        if p and "site-packages" not in p and "dist-packages" not in p and "/local/" not in p
    ]
    sys.path = [source_dir] + keep


def _restrict_syscalls() -> None:
    """Kernel allowlist for a pure selector: no file opens, processes or sockets.

    All source, JSON and permitted stdlib modules are loaded first. Python
    introspection can bypass static checks, but cannot remove this filter.
    Unsupported kernels/libraries refuse execution before candidate code.
    """
    try:
        lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
        lib.seccomp_init.argtypes = [ctypes.c_uint32]
        lib.seccomp_init.restype = ctypes.c_void_p
        lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
        lib.seccomp_rule_add_array.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_void_p,
        ]
        lib.seccomp_rule_add_array.restype = ctypes.c_int
        lib.seccomp_load.argtypes = [ctypes.c_void_p]
        lib.seccomp_load.restype = ctypes.c_int
        lib.seccomp_release.argtypes = [ctypes.c_void_p]
        context = lib.seccomp_init(0x00050001)  # SCMP_ACT_ERRNO(EPERM)
        if not context:
            raise OSError("seccomp_init failed")
        try:
            allowed = (
                "read write writev close fstat newfstatat lseek brk mmap mremap munmap "
                "mprotect madvise futex rt_sigaction rt_sigprocmask rt_sigreturn sigaltstack "
                "getpid gettid clock_gettime gettimeofday time clock_nanosleep nanosleep "
                "sched_yield getrandom getrusage exit exit_group uname fcntl dup dup2 dup3 "
                "poll ppoll select pselect6 getcwd restart_syscall"
            ).split()
            for name in allowed:
                number = lib.seccomp_syscall_resolve_name(name.encode())
                if (
                    number >= 0
                    and lib.seccomp_rule_add_array(
                        context,
                        0x7FFF0000,
                        number,
                        0,
                        None,  # SCMP_ACT_ALLOW
                    )
                    != 0
                ):
                    raise OSError("seccomp rule failed: " + name)
            if lib.seccomp_load(context) != 0:
                raise OSError("seccomp_load failed")
        finally:
            lib.seccomp_release(context)
    except (OSError, AttributeError) as exc:
        raise _LimitsUnavailable(f"required syscall isolation unavailable: {exc}") from exc


def main() -> int:
    payload_path, source_path, cpu_s, mem_mb, nproc = (
        sys.argv[1],
        sys.argv[2],
        int(sys.argv[3]),
        int(sys.argv[4]),
        int(sys.argv[5]),
    )
    try:
        _apply_limits(cpu_s, mem_mb, nproc)
    except _LimitsUnavailable as exc:
        sys.stderr.write(f"hx-limits-unavailable: {exc}\n")
        return 2
    _clean_sys_path(os.path.dirname(os.path.abspath(source_path)))

    saved_out = os.dup(1)
    saved_err = os.dup(2)

    def emit(obj: dict) -> None:
        os.write(saved_out, (json.dumps(obj) + "\n").encode("utf-8"))

    try:
        with open(payload_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        with open(source_path, encoding="utf-8") as fh:
            code = compile(fh.read(), source_path, "exec")
        # This fixed set mirrors the manifest's initial stdlib-only contract.
        # No candidate path is on sys.path while the trusted modules load.
        for name in (
            "collections dataclasses difflib enum functools heapq itertools json math "
            "operator re string typing unicodedata"
        ).split():
            importlib.import_module(name)
        module = types.ModuleType("candidate_selector")
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        try:
            _restrict_syscalls()
            exec(code, module.__dict__)
            func = getattr(module, "select_context", None)
            if not callable(func):
                raise RuntimeError("select_context missing or not callable")
            result = func(payload)
        finally:
            os.dup2(saved_out, 1)
            os.dup2(saved_err, 2)
            os.close(devnull)
        if not isinstance(result, list) or not all(isinstance(i, str) for i in result):
            raise RuntimeError("select_context must return list[str]")
        emit({"ids": result})
    except _LimitsUnavailable as exc:
        os.write(saved_err, f"hx-limits-unavailable: {exc}\n".encode())
        return 2
    except BaseException as exc:  # noqa: BLE001 - candidate must not escape
        try:
            emit({"error": f"candidate_failed: {type(exc).__name__}: {exc}"[:500]})
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
