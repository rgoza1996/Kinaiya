"""Synthetic unit checks for sealed assets, runtime identity and native order.

No model, runtime download, or native inference is used here. Seals use real
memfds; loader assertions use stub torch/transformers modules.
"""

import ast
import hashlib
import importlib.metadata
import importlib.util
import os
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from packages.providers import qwen3_assets, qwen3_ipc


def _synthetic_contents(monkeypatch):
    assets, contents = {}, {}
    for name in qwen3_assets.ASSET_NAMES:
        data = f"synthetic-qwen3-{name}\n".encode()
        assets[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        contents[name] = data
    monkeypatch.setitem(qwen3_assets.MANIFEST, "assets", assets)
    return contents


@pytest.fixture
def sealed_layout(tmp_path, monkeypatch):
    contents = _synthetic_contents(monkeypatch)
    model_dir = tmp_path / "model"
    socket_dir = tmp_path / "sockets"
    workspace = tmp_path / "workspace"
    for directory in (model_dir, socket_dir, workspace):
        directory.mkdir()
    os.chmod(socket_dir, 0o700)
    for name, data in contents.items():
        (model_dir / name).write_bytes(data)
    return model_dir, socket_dir, workspace, contents


def test_sealed_model_roundtrip_and_immutable(sealed_layout):
    model_dir, socket_dir, workspace, contents = sealed_layout
    snapshot = qwen3_ipc.snapshot_path(socket_dir, os.getpid())
    with qwen3_assets.sealed_model(model_dir, workspace, snapshot) as snap:
        assert snap == snapshot
        assert sorted(p.name for p in snap.iterdir()) == sorted(contents)
        for name, data in contents.items():
            link = snap / name
            assert link.is_symlink()
            target = os.readlink(link)
            assert target.startswith("/proc/self/fd/")
            assert (snap / name).read_bytes() == data
        for name in contents:
            with open(model_dir / name, "ab") as handle:
                handle.write(b"later-owner-write")
        for name, data in contents.items():
            assert (snap / name).read_bytes() == data
    assert not snapshot.exists()


@pytest.mark.parametrize("same_size", [False, True])
def test_sealed_model_refuses_bad_digest(sealed_layout, same_size):
    model_dir, socket_dir, workspace, _ = sealed_layout
    original = (model_dir / "config.json").read_bytes()
    (model_dir / "config.json").write_bytes(b"x" * len(original) if same_size else b"tampered")
    with pytest.raises(ValueError):
        with qwen3_assets.sealed_model(
            model_dir, workspace, qwen3_ipc.snapshot_path(socket_dir, os.getpid())
        ):
            pass


def test_sealed_model_refuses_symlink_and_workspace(tmp_path, monkeypatch):
    contents = _synthetic_contents(monkeypatch)
    model_dir = tmp_path / "model"
    socket_dir = tmp_path / "sockets"
    workspace = tmp_path / "workspace"
    for directory in (model_dir, socket_dir, workspace):
        directory.mkdir()
    os.chmod(socket_dir, 0o700)
    real = tmp_path / "real-config"
    real.write_bytes(contents["config.json"])
    (model_dir / "config.json").symlink_to(real)
    for name, data in contents.items():
        if name != "config.json":
            (model_dir / name).write_bytes(data)
    with pytest.raises(ValueError):
        with qwen3_assets.sealed_model(
            model_dir, workspace, qwen3_ipc.snapshot_path(socket_dir, os.getpid())
        ):
            pass
    inner = workspace / "model"
    inner.mkdir()
    with pytest.raises(ValueError):
        with qwen3_assets.sealed_model(
            inner, workspace, qwen3_ipc.snapshot_path(socket_dir, os.getpid())
        ):
            pass


def test_validate_runtime_versions_and_core(tmp_path, monkeypatch):
    root = tmp_path / "site"
    root.mkdir()
    blob_a, blob_b = b"fake-torch-core", b"fake-transformers-core"
    (root / "fake_a.py").write_bytes(blob_a)
    (root / "fake_b.py").write_bytes(blob_b)
    runtime = {
        "python": "3.12",
        "torch": "0.0-fake",
        "transformers": "0.0-fake",
        "device": "cpu",
        "dtype": "float32",
        "intraop_threads": 2,
        "interop_threads": 1,
    }
    core = {
        "torch/fake_a.py": {
            "bytes": len(blob_a),
            "sha256": hashlib.sha256(blob_a).hexdigest(),
        },
        "transformers/fake_b.py": {
            "bytes": len(blob_b),
            "sha256": hashlib.sha256(blob_b).hexdigest(),
        },
    }
    monkeypatch.setattr(qwen3_assets, "RUNTIME", dict(runtime))
    monkeypatch.setattr(qwen3_assets, "RUNTIME_CORE", dict(core))
    monkeypatch.setattr(qwen3_assets.sys, "version_info", (3, 12))
    versions = {"torch": "0.0-fake", "transformers": "0.0-fake"}
    monkeypatch.setattr(importlib.metadata, "version", lambda name: versions[name])
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(submodule_search_locations=[str(root)]),
    )
    assert qwen3_assets.validate_runtime() == runtime
    (root / "fake_a.py").write_bytes(b"x" * len(blob_a))
    with pytest.raises(ValueError, match="core mismatch"):
        qwen3_assets.validate_runtime()
    (root / "fake_a.py").unlink()
    (root / "fake_a.py").symlink_to(root / "fake_b.py")
    with pytest.raises(OSError):
        qwen3_assets.validate_runtime()
    versions["torch"] = "9.9-wrong"
    with pytest.raises(ValueError):
        qwen3_assets.validate_runtime()


def test_worker_native_order_gates():
    module = ast.parse(Path("packages/providers/qwen3_worker.py").read_text())
    source = ast.unparse(
        next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
    )
    order = [
        "listener.listen",
        "deny_network()",
        "sealed_model",
        "tempfile.tempdir",
        "TORCHINDUCTOR_CACHE_DIR",
        "RLIMIT_FSIZE",
        "validate_runtime()",
        "Qwen3Engine",
    ]
    positions = [source.index(token) for token in order]
    assert positions == sorted(positions)
    assert "RLIMIT_CPU" not in source
    assert ".recv(" not in source and ".send(" not in source
    assert "read_frame(" in ast.unparse(module) and "write_frame(" in ast.unparse(module)


def _install_native_fakes(monkeypatch, tmp_path, input_len, texts, out_tokens, last_token=99):
    captured = {}
    fake_torch = types.ModuleType("torch")
    fake_torch.float32 = object()
    fake_torch.set_num_threads = lambda count: captured.setdefault("threads", []).append(count)
    fake_torch.set_num_interop_threads = lambda count: captured.setdefault("interops", []).append(
        count
    )

    @contextmanager
    def inference_mode():
        captured["inference_active"] = True
        try:
            yield
        finally:
            captured["inference_active"] = False

    fake_torch.inference_mode = inference_mode

    class _Ids:
        shape = (1, input_len)

    class _Out:
        shape = (1, input_len + out_tokens)

        def __getitem__(self, key):
            return last_token

    class _BaseStreamer:
        def __init__(self, tokenizer, skip_prompt=True, skip_special_tokens=False):
            self.tokenizer = tokenizer
            captured["skip_special_tokens"] = skip_special_tokens

    fake_transformers = types.ModuleType("transformers")
    fake_transformers.TextStreamer = _BaseStreamer

    class _Tokenizer:
        def apply_chat_template(self, messages, **kwargs):
            captured["chat"] = kwargs
            return "rendered synthetic prompt"

        def __call__(self, text, **kwargs):
            captured["tok_call"] = kwargs
            captured["mask"] = object()
            return {"input_ids": _Ids(), "attention_mask": captured["mask"]}

    class _Model:
        generation_config = SimpleNamespace(eos_token_id=[99])

        def eval(self):
            captured["eval"] = True

        def generate(self, input_ids, **kwargs):
            captured["inference_at_generation"] = captured.get("inference_active", False)
            captured["producer_thread"] = get_ident()
            captured["gen"] = kwargs
            for piece in texts:
                kwargs["streamer"].on_finalized_text(piece)
            if "after_text" in captured:
                captured["after_text"]()
            return _Out()

    class _AutoTokenizer:
        @staticmethod
        def from_pretrained(path, **kwargs):
            captured["tok_load"] = kwargs
            return _Tokenizer()

    class _AutoModel:
        @staticmethod
        def from_pretrained(path, **kwargs):
            captured["model_load"] = kwargs
            captured["model"] = _Model()
            return captured["model"]

    fake_transformers.AutoTokenizer = _AutoTokenizer
    fake_transformers.AutoModelForCausalLM = _AutoModel
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    for name in qwen3_ipc.MANIFEST["assets"]:
        (snapshot / name).write_bytes(b"synthetic")
    return captured, fake_torch, snapshot


def test_engine_loader_arguments(monkeypatch, tmp_path):
    from packages.providers.qwen3_backend import Qwen3Engine

    captured, fake_torch, snapshot = _install_native_fakes(monkeypatch, tmp_path, 5, ["hello"], 2)
    engine = Qwen3Engine(snapshot, max_input_tokens=64, max_new_tokens=16)
    assert captured["threads"] == [2] and captured["interops"] == [1]
    assert captured["tok_load"] == {
        "local_files_only": True,
        "trust_remote_code": False,
        "use_fast": True,
    }
    assert captured["model_load"] == {
        "local_files_only": True,
        "trust_remote_code": False,
        "use_safetensors": True,
        "dtype": fake_torch.float32,
        "device_map": "cpu",
        "attn_implementation": "eager",
    }
    events = list(engine.generate([{"role": "user", "content": "synthetic hello"}]))
    assert captured["chat"] == {
        "tokenize": False,
        "add_generation_prompt": True,
        "enable_thinking": False,
    }
    assert captured["tok_call"] == {"return_tensors": "pt", "add_special_tokens": False}
    assert captured["gen"]["do_sample"] is False
    assert captured["gen"]["num_beams"] == 1
    assert captured["gen"]["max_new_tokens"] == 16
    assert captured["gen"]["use_cache"] is True
    assert captured["gen"]["attention_mask"] is captured["mask"]
    assert captured.get("eval") is True
    assert captured["inference_at_generation"] is True
    assert captured["producer_thread"] != get_ident()
    assert captured["skip_special_tokens"] is True
    assert events[-1]["kind"] == "done"


def test_engine_refuses_oversize_input(monkeypatch, tmp_path):
    from packages.providers.qwen3_backend import Qwen3Engine, Qwen3InputLimitError

    captured, _, snapshot = _install_native_fakes(monkeypatch, tmp_path, 65, ["x"], 1)
    engine = Qwen3Engine(snapshot, max_input_tokens=64, max_new_tokens=16)
    with pytest.raises(Qwen3InputLimitError):
        list(engine.generate([{"role": "user", "content": "too long"}]))
    assert "gen" not in captured


@pytest.mark.parametrize(
    ("count", "last", "expected"), [(16, 99, "eos"), (16, 7, "length"), (1, 99, "eos")]
)
def test_engine_terminal_uses_actual_eos(monkeypatch, tmp_path, count, last, expected):
    from packages.providers.qwen3_backend import Qwen3Engine

    _, _, snapshot = _install_native_fakes(monkeypatch, tmp_path, 4, ["✓"], count, last)
    engine = Qwen3Engine(snapshot, max_input_tokens=64, max_new_tokens=16)
    assert (
        list(engine.generate([{"role": "user", "content": "synthetic"}]))[-1]["finish_reason"]
        == expected
    )


def test_engine_short_non_eos_is_not_completion(monkeypatch, tmp_path):
    from packages.providers.qwen3_backend import Qwen3Engine, Qwen3GenerationError

    _, _, snapshot = _install_native_fakes(monkeypatch, tmp_path, 4, ["partial"], 1, 7)
    engine = Qwen3Engine(snapshot, max_input_tokens=64, max_new_tokens=16)
    with pytest.raises(Qwen3GenerationError, match="without EOS"):
        list(engine.generate([{"role": "user", "content": "synthetic"}]))


def test_engine_never_completes_or_reuses_live_producer(monkeypatch, tmp_path):
    from packages.providers.qwen3_backend import Qwen3Engine, Qwen3GenerationError

    captured, _, snapshot = _install_native_fakes(monkeypatch, tmp_path, 4, ["partial"], 2)
    entered, release = Event(), Event()

    def blocked():
        entered.set()
        assert release.wait(3)

    captured["after_text"] = blocked
    engine = Qwen3Engine(snapshot, max_input_tokens=64, max_new_tokens=16)
    stream = engine.generate([{"role": "user", "content": "synthetic"}])
    try:
        assert next(stream) == {"kind": "delta", "text": "partial"}
        assert entered.wait(1) and engine.producer_alive()
        with pytest.raises(Qwen3GenerationError, match="overlapping"):
            list(engine.generate([{"role": "user", "content": "second"}]))
        stream.close()
        assert engine.producer_alive()
        with pytest.raises(Qwen3GenerationError, match="overlapping"):
            list(engine.generate([{"role": "user", "content": "third"}]))
    finally:
        release.set()
        engine._producer.join(2)
        stream.close()
    assert not engine.producer_alive()


def test_seals_are_enforced_and_descriptors_close(sealed_layout):
    import fcntl

    model_dir, socket_dir, workspace, _ = sealed_layout
    with qwen3_assets.sealed_model(
        model_dir, workspace, qwen3_ipc.snapshot_path(socket_dir, os.getpid())
    ) as snapshot:
        target = int(os.readlink(snapshot / "config.json").rsplit("/", 1)[1])
        seals = fcntl.fcntl(target, fcntl.F_GET_SEALS)
        assert (
            seals & fcntl.F_SEAL_WRITE
            and seals & fcntl.F_SEAL_GROW
            and seals & fcntl.F_SEAL_SHRINK
            and seals & fcntl.F_SEAL_SEAL
        )
        with pytest.raises(PermissionError):
            os.write(target, b"changed")
    with pytest.raises(OSError):
        os.fstat(target)


def test_snapshot_cleanup_spares_replaced_directory(sealed_layout):
    model_dir, socket_dir, workspace, _ = sealed_layout
    snapshot = qwen3_ipc.snapshot_path(socket_dir, os.getpid())
    moved = socket_dir / "original-snapshot"
    with qwen3_assets.sealed_model(model_dir, workspace, snapshot):
        snapshot.rename(moved)
        snapshot.mkdir()
        sentinel = snapshot / "sentinel"
        sentinel.write_text("keep")
    assert sentinel.read_text() == "keep"
    assert not list(moved.iterdir())


def test_failed_sealing_closes_prior_descriptors(sealed_layout, monkeypatch):
    model_dir, socket_dir, workspace, _ = sealed_layout
    opened = []
    original = qwen3_assets._sealed_fd

    def fail_second(*args):
        if opened:
            raise ValueError("synthetic next-file failure")
        fd = original(*args)
        opened.append(fd)
        return fd

    monkeypatch.setattr(qwen3_assets, "_sealed_fd", fail_second)
    snapshot = qwen3_ipc.snapshot_path(socket_dir, os.getpid())
    with pytest.raises(ValueError, match="next-file"):
        with qwen3_assets.sealed_model(model_dir, workspace, snapshot):
            pytest.fail("unverified snapshot yielded")
    assert not snapshot.exists()
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_snapshot_cleanup_preserves_replaced_link(sealed_layout):
    model_dir, socket_dir, workspace, _ = sealed_layout
    snapshot = qwen3_ipc.snapshot_path(socket_dir, os.getpid())
    with qwen3_assets.sealed_model(model_dir, workspace, snapshot):
        link = snapshot / "config.json"
        target = os.readlink(link)
        link.unlink()
        link.symlink_to(target)
    assert link.is_symlink() and os.readlink(link) == target


def test_snapshot_must_stay_outside_agent_workspace(sealed_layout):
    model_dir, _socket_dir, workspace, _ = sealed_layout
    private = workspace / "private"
    private.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="outside the Agent workspace"):
        with qwen3_assets.sealed_model(
            model_dir, workspace, qwen3_ipc.snapshot_path(private, os.getpid())
        ):
            pytest.fail("snapshot inside Agent workspace yielded")


def test_main_seals_before_zero_file_limit_and_imports(sealed_layout, monkeypatch):
    import resource

    from packages.providers import qwen3_backend, qwen3_worker

    model_dir, socket_dir, workspace, contents = sealed_layout
    steps = []

    class Listener:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, path):
            steps.append("bound")

        def listen(self, count):
            steps.append("listening")

    def limit(which, values):
        if which == resource.RLIMIT_FSIZE:
            assert values == (0, 0)
            assert qwen3_worker.tempfile.gettempdir() == str(socket_dir)
            assert os.environ["TORCHINDUCTOR_CACHE_DIR"] == str(socket_dir)
            snapshot = qwen3_ipc.snapshot_path(socket_dir, os.getpid())
            assert {p.name for p in snapshot.iterdir()} == set(contents)
            assert all((snapshot / name).read_bytes() == data for name, data in contents.items())
            steps.append("sealed_before_zero_file_limit")

    def runtime():
        assert steps[-1] == "sealed_before_zero_file_limit"
        steps.append("validated_runtime")
        return dict(qwen3_ipc.MANIFEST["runtime"])

    def engine(snapshot, **limits):
        assert steps[-1] == "validated_runtime"
        steps.append("loaded_model")
        return object()

    monkeypatch.setattr(qwen3_worker.resource, "setrlimit", limit)
    monkeypatch.setattr(qwen3_worker.os, "sched_getaffinity", lambda pid: {0, 1})
    monkeypatch.setattr(qwen3_worker.os, "sched_setaffinity", lambda pid, cpus: None)
    monkeypatch.setattr(qwen3_worker.socket, "socket", lambda *args: Listener())
    monkeypatch.setattr(qwen3_worker, "deny_network", lambda: steps.append("fenced") or "synthetic")
    monkeypatch.setattr(qwen3_assets, "validate_runtime", runtime)
    monkeypatch.setattr(qwen3_backend, "Qwen3Engine", engine)
    monkeypatch.setattr(qwen3_worker, "serve", lambda *args: steps.append("served"))
    monkeypatch.setattr(qwen3_worker.tempfile, "tempdir", None)
    monkeypatch.setenv("TORCHINDUCTOR_CACHE_DIR", "/unselected-owner-cache")
    monkeypatch.setattr(sys, "dont_write_bytecode", sys.dont_write_bytecode)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker",
            "--socket-dir",
            str(socket_dir),
            "--model-dir",
            str(model_dir),
            "--workspace",
            str(workspace),
        ],
    )
    qwen3_worker.main()
    assert steps == [
        "bound",
        "listening",
        "fenced",
        "sealed_before_zero_file_limit",
        "validated_runtime",
        "loaded_model",
        "served",
    ]
    assert not qwen3_ipc.snapshot_path(socket_dir, os.getpid()).exists()
