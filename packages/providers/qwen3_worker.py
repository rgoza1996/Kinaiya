"""Resources-owned direct Qwen3 CPU dialogue child; only framed private Unix IPC.

Fixed worker contract: the listener is created before the network fence, the
sealed snapshot is prepared before RLIMIT_FSIZE(0,0), and the optional native
runtime is imported and loaded only after both fences. No cumulative CPU cap
applies to this resident child; the parent owns every deadline.
"""

import argparse
import os
import resource
import secrets
import socket
import stat
import sys
import tempfile
from contextlib import closing

from packages.providers import qwen3_ipc
from packages.providers.qwen3_ipc import (
    MAX_EVENTS,
    MAX_FRAME_BYTES,
    MAX_REQUEST_BYTES,
    MAX_TEXT_BYTES,
    decode_json,
    encode_json,
    event_message,
    parse_request,
    peer_pid,
    private_directory,
    read_frame,
    socket_path,
    write_frame,
)
from packages.providers.whistle_isolation import deny_network

INPUT_MODE = "sealed-safetensors-v1"
MAX_WIRE_BYTES = 2 * 1024 * 1024


def _producer_live(engine):
    try:
        probe = getattr(engine, "producer_alive", None)
        # An unavailable or malformed probe cannot confirm native cleanup.
        return probe() is not False if callable(probe) else True
    except Exception:
        return True


def serve(
    directory,
    engine,
    incarnation,
    isolation,
    listener,
    parent_pid,
    runtime,
    max_input_tokens=2048,
    max_new_tokens=128,
):
    from packages.providers.qwen3_backend import (
        Qwen3GenerationError,
        Qwen3InputLimitError,
        Qwen3OutputLimitError,
    )

    while True:
        connection, _ = listener.accept()
        with connection:
            try:
                # SO_PEERCRED bound to the original parent PID/UID, every peer.
                peer_pid(connection, parent_pid)
                fd = connection.fileno()
                wire_bytes = 0

                def send(event, output_fd=fd):
                    nonlocal wire_bytes
                    raw = encode_json(event, MAX_FRAME_BYTES)
                    if wire_bytes + len(raw) + 4 > MAX_WIRE_BYTES:
                        raise Qwen3OutputLimitError("Qwen3 stream exceeds its wire bound")
                    wire_bytes += len(raw) + 4
                    write_frame(output_fd, raw, MAX_FRAME_BYTES)

                hello = {
                    "version": qwen3_ipc.VERSION,
                    "kind": "hello",
                    "pid": os.getpid(),
                    "incarnation": incarnation,
                    "profile": qwen3_ipc.PROFILE,
                    "model": qwen3_ipc.MODEL,
                    "revision": qwen3_ipc.REVISION,
                    "manifest_sha256": qwen3_ipc.MANIFEST_SHA256,
                    "runtime": dict(runtime),
                    "isolation": isolation,
                    "input_mode": INPUT_MODE,
                    "max_input_tokens": max_input_tokens,
                    "max_new_tokens": max_new_tokens,
                }
                send(hello)
                request = parse_request(
                    decode_json(read_frame(fd, MAX_REQUEST_BYTES), MAX_REQUEST_BYTES), incarnation
                )
                nonce = request["nonce"]
                if request["kind"] == "probe":
                    ready = event_message("ready", os.getpid(), incarnation, nonce)
                    send(ready)
                    continue
                seq, decoded_total = 0, 0
                try:
                    terminal = None
                    # Closing the iterator must precede terminal publication.
                    # A transport exception also unwinds this ownership path.
                    with closing(engine.generate(request["messages"])) as generation:
                        for item in generation:
                            kind = item.get("kind") if isinstance(item, dict) else None
                            if kind == "delta":
                                text = item.get("text")
                                size = len(text.encode("utf-8")) if isinstance(text, str) else 0
                                if (
                                    not isinstance(text, str)
                                    or not 1 <= size <= qwen3_ipc.MAX_DELTA_BYTES
                                    or seq >= MAX_EVENTS - 1
                                ):
                                    raise Qwen3OutputLimitError("Invalid bounded Qwen text delta")
                                decoded_total += size
                                if decoded_total > MAX_TEXT_BYTES:
                                    raise Qwen3OutputLimitError(
                                        "Qwen3 output exceeds its text bound"
                                    )
                                send(
                                    event_message(
                                        "delta", os.getpid(), incarnation, nonce, seq=seq, text=text
                                    )
                                )
                                seq += 1
                            elif kind == "done":
                                terminal = item
                                break
                            else:
                                raise Qwen3GenerationError("Qwen3 emitted no terminal result")
                    if _producer_live(engine):
                        os._exit(1)
                    if terminal is None:
                        raise Qwen3GenerationError("Qwen3 emitted no terminal result")
                    event = event_message(
                        "done",
                        os.getpid(),
                        incarnation,
                        nonce,
                        seq=seq,
                        finish_reason=terminal["finish_reason"],
                        input_tokens=terminal["input_tokens"],
                        output_tokens=terminal["output_tokens"],
                    )
                    qwen3_ipc.parse_event(
                        event,
                        os.getpid(),
                        incarnation,
                        nonce,
                        seq=seq,
                        max_input_tokens=max_input_tokens,
                        max_new_tokens=max_new_tokens,
                    )
                    send(event)
                except (OSError, EOFError):
                    raise
                except Exception as exc:
                    if _producer_live(engine):
                        os._exit(1)
                    code = (
                        "input_limit"
                        if isinstance(exc, Qwen3InputLimitError)
                        else "output_limit"
                        if isinstance(exc, Qwen3OutputLimitError)
                        else "generation_failed"
                    )
                    try:
                        send(
                            event_message(
                                "error", os.getpid(), incarnation, nonce, seq=seq, code=code
                            )
                        )
                    except (OSError, EOFError, ValueError):
                        pass
            except (OSError, EOFError, ValueError):
                if _producer_live(engine):
                    # Closing transport is not proof the native thread
                    # stopped; terminate this dedicated child instead of
                    # risking uncertain replay or overlapping generation.
                    os._exit(1)
                continue
            finally:
                if _producer_live(engine):
                    os._exit(1)


def main():
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket-dir", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--cpu-count", type=int, default=2)
    parser.add_argument("--memory-bytes", type=int, default=16 * 1024**3)
    parser.add_argument("--max-input-tokens", type=int, default=2048)
    parser.add_argument("--max-new-tokens", type=int, default=128)
    args = parser.parse_args()
    limits = qwen3_ipc.validate_limits(args.max_input_tokens, args.max_new_tokens)
    if args.cpu_count != 2:
        raise ValueError("Qwen3 worker requires exactly two CPUs")
    if not 8 * 1024**3 <= args.memory_bytes <= 32 * 1024**3:
        raise ValueError("Qwen3 address-space limit must be 8..32 GiB")
    directory = private_directory(args.socket_dir)
    parent_pid = os.getppid()
    cpus = sorted(os.sched_getaffinity(0))[: args.cpu_count]
    if len(cpus) != args.cpu_count:
        raise ValueError("Qwen3 requires two available CPUs")
    os.sched_setaffinity(0, cpus)
    resource.setrlimit(resource.RLIMIT_AS, (args.memory_bytes, args.memory_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # No cumulative RLIMIT_CPU: the parent owns a finite deadline per request.
    path = socket_path(directory, os.getpid())
    if path.exists():
        info = path.lstat()
        # Only this PID exists now; a same-PID private socket is a stale entry.
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("Unsafe Qwen3 socket entry")
        path.unlink()
    os.umask(0o077)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen(8)
        # Fence first: no new network channels before optional runtime code.
        isolation = deny_network()
        from packages.providers.qwen3_assets import sealed_model, validate_runtime

        snapshot = qwen3_ipc.snapshot_path(directory, os.getpid())
        # Sealed copies and links first; the file-size fence also covers
        # memfd writes, so it must land before any native import or load.
        with sealed_model(args.model_dir, args.workspace, snapshot) as sealed:
            # First-use tempfile discovery writes a probe, which FSIZE(0)
            # correctly refuses. Select this existing private root explicitly;
            # Torch's import-time cache_dir() needs only makedirs(exist_ok).
            # Eager inference gets no global cache or writable cache exception.
            tempfile.tempdir = str(directory)
            os.environ["TORCHINDUCTOR_CACHE_DIR"] = str(directory)
            resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
            runtime = validate_runtime()
            from packages.providers.qwen3_backend import Qwen3Engine

            engine = Qwen3Engine(
                sealed,
                max_input_tokens=limits["max_input_tokens"],
                max_new_tokens=limits["max_new_tokens"],
            )
            serve(
                directory,
                engine,
                secrets.token_hex(32),
                isolation,
                listener,
                parent_pid,
                runtime,
                limits["max_input_tokens"],
                limits["max_new_tokens"],
            )


if __name__ == "__main__":
    main()
