"""Resources-owned direct batch STT child; only framed private Unix IPC."""

import argparse
import os
import resource
import secrets
import socket
import stat

from packages.providers.whistle_assets import MANIFEST, PROFILE
from packages.providers.whistle_audio import MAX_AUDIO_BYTES
from packages.providers.whistle_ipc import (
    MAX_CONTROL_BYTES,
    MAX_RESPONSE_BYTES,
    VERSION,
    decode_json,
    encode_json,
    peer_pid,
    private_directory,
    read_frame,
    socket_path,
    token,
    write_frame,
)
from packages.providers.whistle_isolation import deny_network
from packages.providers.whistle_stream import MAX_CHUNK_SAMPLES


def serve(directory, engine, incarnation, isolation, listener, parent_pid):
    while True:
        connection, _ = listener.accept()
        with connection:
            try:
                peer_pid(connection, parent_pid)
                fd = connection.fileno()
                hello = {
                    "version": VERSION,
                    "kind": "hello",
                    "pid": os.getpid(),
                    "incarnation": incarnation,
                    "profile": PROFILE,
                    "isolation": isolation,
                    "weights_sha256": MANIFEST["weights"]["sha256"],
                    "library_sha256": MANIFEST["library"]["sha256"],
                }
                write_frame(fd, encode_json(hello), MAX_CONTROL_BYTES)
                request = decode_json(read_frame(fd, MAX_CONTROL_BYTES))
                if (
                    set(request) != {"version", "kind", "incarnation", "nonce"}
                    or type(request["version"]) is not int
                    or request["version"] != VERSION
                    or request["incarnation"] != incarnation
                    or not token(request["nonce"])
                    or not isinstance(request["kind"], str)
                    or request["kind"]
                    not in {
                        "probe",
                        "transcribe",
                        "stream_begin",
                        "stream_pcm",
                        "stream_finish",
                    }
                ):
                    raise ValueError("Invalid Whistle request identity")
                kind = request["kind"]
                response_kind = (
                    "ready"
                    if kind == "probe"
                    else (
                        "stream_ready"
                        if kind == "stream_begin"
                        else ("stream_result" if kind.startswith("stream_") else "result")
                    )
                )
                result = {
                    "version": VERSION,
                    "kind": response_kind,
                    "pid": os.getpid(),
                    "incarnation": incarnation,
                    "nonce": request["nonce"],
                }
                if kind == "transcribe":
                    data = read_frame(fd, MAX_AUDIO_BYTES)
                    try:
                        result["text"] = engine.transcribe(data)
                    except Exception:
                        # Do not copy native error text/audio/transcripts into logs.
                        result["error"] = "transcription_failed"
                elif kind == "stream_begin":
                    try:
                        engine.begin_stream()
                    except Exception:
                        result["error"] = "stream_failed"
                elif kind == "stream_pcm":
                    pcm16 = read_frame(fd, MAX_CHUNK_SAMPLES * 2)
                    try:
                        result["update"] = engine.stream_pcm(pcm16)
                    except Exception:
                        result["error"] = "stream_failed"
                elif kind == "stream_finish":
                    try:
                        result["update"] = engine.finish_stream()
                    except Exception:
                        result["error"] = "stream_failed"
                write_frame(fd, encode_json(result, MAX_RESPONSE_BYTES), MAX_RESPONSE_BYTES)
            except (OSError, EOFError, ValueError):
                continue


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket-dir", required=True)
    parser.add_argument("--weights", required=True)
    parser.add_argument("--library", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--cpu-count", type=int, default=2, choices=range(1, 5))
    parser.add_argument("--memory-bytes", type=int, default=1 << 30)
    args = parser.parse_args()
    directory = private_directory(args.socket_dir)
    parent_pid = os.getppid()
    cpus = sorted(os.sched_getaffinity(0))[: args.cpu_count]
    os.sched_setaffinity(0, cpus)
    if not 256 * 1024**2 <= args.memory_bytes <= 4 * 1024**3:
        raise ValueError("Invalid Whistle address-space limit")
    resource.setrlimit(resource.RLIMIT_AS, (args.memory_bytes, args.memory_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Engine preparation seals both inputs before disabling file growth;
    # RLIMIT_FSIZE also applies to the memfds used for those bounded copies.
    # No cumulative RLIMIT_CPU: the parent owns a finite deadline per request.
    path = socket_path(directory, os.getpid())
    if path.exists():
        info = path.lstat()
        # Only this PID exists now; a same-PID private socket is a stale entry.
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("Unsafe Whistle socket entry")
        path.unlink()
    os.umask(0o077)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen(1)
        isolation = deny_network()
        from packages.providers.whistle_backend import WhistleEngine

        engine = WhistleEngine(args.weights, args.library, args.workspace)
        serve(directory, engine, secrets.token_hex(32), isolation, listener, parent_pid)


if __name__ == "__main__":
    main()
