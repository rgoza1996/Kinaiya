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

import importlib.util
import json
import os
import sys


def _apply_limits(cpu_s: int, mem_mb: int, nproc: int) -> None:
    try:
        import resource
    except ImportError:
        return
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        as_bytes = mem_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 20, 1 << 20))
        resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass


def _clean_sys_path(source_dir: str) -> None:
    keep = [
        p
        for p in sys.path
        if p
        and "site-packages" not in p
        and "dist-packages" not in p
        and "/local/" not in p
    ]
    sys.path = [source_dir] + keep


def main() -> int:
    payload_path, source_path, cpu_s, mem_mb, nproc = (
        sys.argv[1],
        sys.argv[2],
        int(sys.argv[3]),
        int(sys.argv[4]),
        int(sys.argv[5]),
    )
    _apply_limits(cpu_s, mem_mb, nproc)
    _clean_sys_path(os.path.dirname(os.path.abspath(source_path)))

    saved_out = os.dup(1)
    saved_err = os.dup(2)

    def emit(obj: dict) -> None:
        os.write(saved_out, (json.dumps(obj) + "\n").encode("utf-8"))

    try:
        with open(payload_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        spec = importlib.util.spec_from_file_location(
            "candidate_selector", source_path
        )
        if spec is None or spec.loader is None:
            emit({"error": "candidate_failed: cannot load source"})
            return 0
        module = importlib.util.module_from_spec(spec)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        try:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            func = getattr(module, "select_context", None)
            if not callable(func):
                raise RuntimeError("select_context missing or not callable")
            result = func(payload)
        finally:
            os.dup2(saved_out, 1)
            os.dup2(saved_err, 2)
            os.close(devnull)
        if not isinstance(result, list) or not all(
            isinstance(i, str) for i in result
        ):
            raise RuntimeError("select_context must return list[str]")
        emit({"ids": result})
    except BaseException as exc:  # noqa: BLE001 - candidate must not escape
        try:
            emit({"error": f"candidate_failed: {type(exc).__name__}: {exc}"[:500]})
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
