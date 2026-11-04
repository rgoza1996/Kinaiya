"""Production worker wiring with synthetic OS/engine seams; no native load."""

from types import SimpleNamespace

import pytest

from packages.providers import whistle_backend, whistle_worker


def test_production_worker_enables_streaming_after_isolation_and_keeps_batch(monkeypatch, tmp_path):
    directory = tmp_path / "ipc"
    directory.mkdir(mode=0o700)
    events = []
    engines = []

    class Finished(Exception):
        pass

    class Listener:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append("listener_closed")

        def bind(self, path):
            assert path.startswith(str(directory))
            events.append("bound")

        def listen(self, backlog):
            assert backlog == 1

    class Engine:
        def __init__(self, weights, library, workspace, *, enable_streaming=False):
            assert events[-1] == "network_denied"
            assert (weights, library, workspace) == (
                "synthetic-weights",
                "synthetic-library",
                "synthetic-workspace",
            )
            self.enabled = enable_streaming
            self.active = False
            self.samples = 0
            engines.append(self)
            events.append("engine_constructed")

        def transcribe(self, data):
            assert not self.active
            return "synthetic batch"

        def begin_stream(self):
            assert self.enabled is True
            self.active = True

        def stream_pcm(self, pcm):
            assert self.active
            self.samples += len(pcm) // 2
            return self.samples

        def finish_stream(self):
            assert self.active
            self.active = False
            return self.samples

    def deny_network():
        events.append("network_denied")
        return "seccomp-network-v1"

    def serve(got_directory, engine, incarnation, isolation, listener, parent_pid):
        assert got_directory == directory
        assert isolation == "seccomp-network-v1"
        assert len(incarnation) == 64
        assert engine.enabled is True
        assert engine.transcribe(b"synthetic") == "synthetic batch"
        engine.begin_stream()
        assert engine.stream_pcm(b"\0\0") == 1
        assert engine.finish_stream() == 1
        assert engine.transcribe(b"synthetic") == "synthetic batch"
        events.append("served")
        raise Finished

    monkeypatch.setattr(
        whistle_worker,
        "os",
        SimpleNamespace(
            getppid=lambda: 11,
            getpid=lambda: 12,
            sched_getaffinity=lambda pid: {0, 1, 2},
            sched_setaffinity=lambda pid, cpus: events.append(("affinity", cpus)),
            umask=lambda mask: events.append(("umask", mask)),
        ),
    )
    monkeypatch.setattr(
        whistle_worker,
        "resource",
        SimpleNamespace(
            RLIMIT_AS=1,
            RLIMIT_CORE=2,
            setrlimit=lambda which, limits: events.append(("limit", which, limits)),
        ),
    )
    monkeypatch.setattr(
        whistle_worker,
        "socket",
        SimpleNamespace(AF_UNIX=1, SOCK_STREAM=1, socket=lambda *args: Listener()),
    )
    monkeypatch.setattr(whistle_worker, "deny_network", deny_network)
    monkeypatch.setattr(whistle_backend, "WhistleEngine", Engine)
    monkeypatch.setattr(whistle_worker, "serve", serve)
    monkeypatch.setattr(
        "sys.argv",
        [
            "whistle_worker",
            "--socket-dir",
            str(directory),
            "--weights",
            "synthetic-weights",
            "--library",
            "synthetic-library",
            "--workspace",
            "synthetic-workspace",
        ],
    )
    with pytest.raises(Finished):
        whistle_worker.main()
    assert len(engines) == 1
    assert events[:3] == [
        ("affinity", [0, 1]),
        ("limit", 1, (1 << 30, 1 << 30)),
        ("limit", 2, (0, 0)),
    ]
    assert events[-2:] == ["served", "listener_closed"]
