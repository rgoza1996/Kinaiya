"""Synthetic GGUF boundary checks; no real weights, models, or native exec."""

import hashlib
import json
import os
import resource
from contextlib import ExitStack
from pathlib import Path

import pytest

from packages.providers import gguf_assets, gguf_worker


def _asset_file(root, name, data=b"x"):
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    blob = (b"synthetic-gguf-" + name.encode() + b"\n") if data == b"x" else data
    target.write_bytes(blob)
    return target, blob


def _manifest_bytes(owner, workspace, sampling=None, alias="test/model-1", guests=None):
    owner.mkdir(parents=True, exist_ok=True)
    entries = []
    if guests is None:
        for guest, name in [
            ("/model.gguf", "model.bin"),
            ("/runtime/llama-server", "llama-server"),
            ("/vendor/libx.so", "libx.so"),
        ]:
            target, blob = _asset_file(owner, name)
            entries.append(
                {
                    "path": str(target),
                    "guest": guest,
                    "bytes": len(blob),
                    "sha256": hashlib.sha256(blob).hexdigest(),
                }
            )
    else:
        entries = guests
    manifest = {
        "version": 1,
        "profile": "synthetic-gguf-cpu",
        "model": "synthetic-test-model",
        "alias": alias,
        "assets": entries,
        "sampling": sampling
        or {
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 40,
            "min_p": 0.05,
            "presence_penalty": 0.0,
            "repeat_penalty": 1.1,
        },
    }
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return manifest, raw, entries


def _write_manifest(tmp_path, owner_name="owner", sampling=None, alias="test/model-1", guests=None):
    owner = tmp_path / owner_name
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    manifest, raw, entries = _manifest_bytes(owner, workspace, sampling, alias, guests)
    path = tmp_path / "gguf.json"
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest(), workspace, entries


def test_load_roundtrip(tmp_path):
    path, digest, workspace, _ = _write_manifest(tmp_path)
    out = gguf_assets.load_manifest(str(path), digest, workspace)
    assert out["alias"] == "test/model-1"
    assert len(out["assets"]) == 3


def test_load_refuses_oversize_digest_and_type(tmp_path):
    path, digest, workspace, _ = _write_manifest(tmp_path)
    with pytest.raises(ValueError):
        gguf_assets.load_manifest(str(path), "0" * 64, workspace)
    with pytest.raises(ValueError):
        gguf_assets.load_manifest(str(path), "ZZ", workspace)
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (64 * 1024 + 1))
    with pytest.raises(ValueError):
        gguf_assets.load_manifest(str(big), "0" * 64, workspace)


def test_load_refuses_duplicate_nan_unknown(tmp_path):
    _, _, workspace, _ = _write_manifest(tmp_path)
    dup = tmp_path / "dup.json"
    dup.write_bytes(
        b'{"version":1,"version":2,"profile":"a","model":"m","alias":"a","assets":[],"sampling":{}}'
    )
    with pytest.raises(ValueError):
        gguf_assets.load_manifest(str(dup), hashlib.sha256(dup.read_bytes()).hexdigest(), workspace)
    nan = tmp_path / "nan.json"
    nan.write_bytes(
        b'{"version":NaN,"profile":"a","model":"m","alias":"a","assets":[],"sampling":{}}'
    )
    with pytest.raises(ValueError):
        gguf_assets.load_manifest(str(nan), hashlib.sha256(nan.read_bytes()).hexdigest(), workspace)
    _, raw, _ = _manifest_bytes(tmp_path / "o2", tmp_path / "w2")
    doc = json.loads(raw.decode())
    doc["extra"] = 1
    extra = tmp_path / "extra.json"
    extra.write_bytes(json.dumps(doc).encode())
    with pytest.raises(ValueError):
        gguf_assets.load_manifest(
            str(extra), hashlib.sha256(extra.read_bytes()).hexdigest(), tmp_path / "w2"
        )


def test_load_refuses_alias_and_names(tmp_path):
    path, _, workspace, _ = _write_manifest(tmp_path)
    raw = json.loads(path.read_bytes().decode())
    for bad in ["", "x" * 129, "bad\nname"]:
        doc = dict(raw)
        doc["alias"] = bad
        p = tmp_path / "badalias.json"
        p.write_bytes(json.dumps(doc).encode())
        with pytest.raises(ValueError):
            gguf_assets.load_manifest(str(p), hashlib.sha256(p.read_bytes()).hexdigest(), workspace)


def test_load_refuses_guest_rules(tmp_path):
    owner = tmp_path / "owner"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    def _case(entries):
        manifest = {
            "version": 1,
            "profile": "p",
            "model": "m",
            "alias": "a/b",
            "assets": entries,
            "sampling": {
                "temperature": 0.5,
                "top_p": 0.9,
                "top_k": 40,
                "min_p": 0.0,
                "presence_penalty": 0.0,
                "repeat_penalty": 1.0,
            },
        }
        p = tmp_path / "m.json"
        p.write_bytes(json.dumps(manifest).encode())
        with pytest.raises(ValueError):
            gguf_assets.load_manifest(str(p), hashlib.sha256(p.read_bytes()).hexdigest(), workspace)

    t1, b1 = _asset_file(owner, "m1.bin")

    def good(g):
        return {
            "path": str(t1),
            "guest": g,
            "bytes": len(b1),
            "sha256": hashlib.sha256(b1).hexdigest(),
        }

    # missing server
    _case([good("/model.gguf")])
    # duplicate guest
    _case([good("/model.gguf"), good("/model.gguf"), good("/runtime/llama-server")])
    # subdir guest
    t2, b2 = _asset_file(owner, "s.bin")
    _case(
        [
            good("/model.gguf"),
            {
                "path": str(t2),
                "guest": "/runtime/sub/dir",
                "bytes": len(b2),
                "sha256": hashlib.sha256(b2).hexdigest(),
            },
        ]
    )
    # unsafe basename
    _case(
        [
            good("/model.gguf"),
            {
                "path": str(t2),
                "guest": "/vendor/../evil",
                "bytes": len(b2),
                "sha256": hashlib.sha256(b2).hexdigest(),
            },
        ]
    )


def test_load_refuses_sampling(tmp_path):
    path, _, workspace, _ = _write_manifest(tmp_path)
    base = json.loads(path.read_bytes().decode())
    for key, bad in [
        ("temperature", 5),
        ("top_p", 2),
        ("top_k", 2000),
        ("min_p", -1),
        ("presence_penalty", 5),
        ("repeat_penalty", 0.05),
        ("temperature", True),
    ]:
        doc = json.loads(json.dumps(base))
        doc["sampling"][key] = bad
        p = tmp_path / "samp.json"
        p.write_bytes(json.dumps(doc).encode())
        with pytest.raises(ValueError):
            gguf_assets.load_manifest(str(p), hashlib.sha256(p.read_bytes()).hexdigest(), workspace)
    doc = json.loads(json.dumps(base))
    del doc["sampling"]["temperature"]
    p = tmp_path / "samp2.json"
    p.write_bytes(json.dumps(doc).encode())
    with pytest.raises(ValueError):
        gguf_assets.load_manifest(str(p), hashlib.sha256(p.read_bytes()).hexdigest(), workspace)


def test_load_refuses_symlink_workspace_and_size(tmp_path):
    owner = tmp_path / "owner"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    target, blob = _asset_file(owner, "model.bin")
    link = owner / "link.bin"
    try:
        link.symlink_to(target)
    except OSError:
        pass
    else:
        manifest = {
            "version": 1,
            "profile": "p",
            "model": "m",
            "alias": "a",
            "assets": [
                {
                    "path": str(link),
                    "guest": "/model.gguf",
                    "bytes": len(blob),
                    "sha256": hashlib.sha256(blob).hexdigest(),
                },
                {
                    "path": str(target),
                    "guest": "/runtime/llama-server",
                    "bytes": len(blob),
                    "sha256": hashlib.sha256(blob).hexdigest(),
                },
            ],
            "sampling": {
                "temperature": 0.5,
                "top_p": 0.9,
                "top_k": 40,
                "min_p": 0.0,
                "presence_penalty": 0.0,
                "repeat_penalty": 1.0,
            },
        }
        p = tmp_path / "lnk.json"
        p.write_bytes(json.dumps(manifest).encode())
        with pytest.raises(ValueError):
            gguf_assets.load_manifest(str(p), hashlib.sha256(p.read_bytes()).hexdigest(), workspace)


def test_sealing_immutability(tmp_path):
    from packages.providers.whistle_assets import sealed_asset

    owner = tmp_path / "owner"
    workspace = tmp_path / "workspace"
    owner.mkdir()
    workspace.mkdir()
    target = owner / "blob.bin"
    blob = b"synthetic-sealed-bytes"
    target.write_bytes(blob)
    pin = {"bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
    with sealed_asset(str(target), pin, workspace) as fd:
        assert fd >= 0
        target.write_bytes(blob + b"later-owner-write")
        os.lseek(fd, 0, os.SEEK_SET)
        assert os.read(fd, 1024 * 1024) == blob


def test_build_args_fixed_and_isolated(tmp_path, monkeypatch):
    import services.agent.tools as agent_tools

    seen = {}
    real = agent_tools.build_bwrap_args

    def _spy(policy, command, network):
        seen["workspace"] = Path(policy.workspace)
        seen["network"] = network
        seen["command"] = command
        return real(policy, command, network)

    monkeypatch.setattr(agent_tools, "build_bwrap_args", _spy)
    path, digest, workspace, _ = _write_manifest(tmp_path)
    manifest = gguf_assets.load_manifest(str(path), digest, workspace)
    socket_dir = tmp_path / "sock"
    socket_dir.mkdir()
    os.chmod(socket_dir, 0o700)
    (socket_dir / "ipc").mkdir()
    os.chmod(socket_dir / "ipc", 0o700)
    from packages.providers.whistle_assets import sealed_asset

    with ExitStack() as stack:
        sealed = {
            e["guest"]: stack.enter_context(sealed_asset(e["path"], e, workspace))
            for e in manifest["assets"]
        }
        argv = gguf_worker.build_args(manifest, sealed, socket_dir)
    assert seen["network"] is False
    assert seen["workspace"] == socket_dir / "ipc"
    for flag in [
        "--unshare-user",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--unshare-cgroup",
        "--unshare-net",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--clearenv",
        "--die-with-parent",
        "--chdir",
        "/workspace",
    ]:
        assert flag in argv, flag
    # fixed native argv present in shell command
    cmd = seen["command"]
    assert cmd.startswith("exec ")
    assert "/runtime/llama-server" in cmd and "--model" in cmd and "/model.gguf" in cmd
    assert "--gpu-layers" in cmd and "--device none" in cmd
    assert "--chat-template-kwargs" in cmd and "--log-disable" in cmd
    # Exact links to retained sealed FDs, no broad binds or FD-consuming copies.
    for guest, fd in sealed.items():
        idx = argv.index(guest)
        assert argv[idx - 2] == "--symlink" and argv[idx - 1] == f"/proc/self/fd/{fd}"
    assert not {"--ro-bind-fd", "--bind-fd", "--file", "--bind-data", "--ro-bind-data"} & set(argv)
    pairs = [(argv[i], argv[i + 1]) for i, a in enumerate(argv[:-1]) if a == "--ro-bind"]
    assert all(dst not in ("/runtime", "/vendor") for _, dst in pairs)
    assert "--dir" in argv
    assert "--setenv" in argv and "LD_LIBRARY_PATH" in argv
    assert "HF_HUB_OFFLINE" in argv
    # no secrets in argv
    joined = " ".join(argv) + cmd
    for secret in ("API_KEY", "TOKEN", "SECRET", "Authorization"):
        assert secret not in joined


def test_build_args_rejects_mismatch_and_counts(tmp_path):
    path, digest, workspace, _ = _write_manifest(tmp_path)
    manifest = gguf_assets.load_manifest(str(path), digest, workspace)
    socket_dir = tmp_path / "sock"
    socket_dir.mkdir()
    os.chmod(socket_dir, 0o700)
    (socket_dir / "ipc").mkdir()
    os.chmod(socket_dir / "ipc", 0o700)
    with pytest.raises(ValueError):
        gguf_worker.build_args(manifest, {}, socket_dir)
    with pytest.raises(ValueError):
        gguf_worker.build_args(
            manifest, {e["guest"]: 3 for e in manifest["assets"]}, socket_dir, cpu_count=1
        )
    with pytest.raises(ValueError):
        gguf_worker.build_args(
            manifest, {e["guest"]: 3 for e in manifest["assets"]}, socket_dir, ctx_size=10
        )


@pytest.fixture
def mocked_setup(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "umask", lambda value: calls.append(("umask", value)))
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {2, 4, 6})
    monkeypatch.setattr(os, "sched_setaffinity", lambda pid, cpus: calls.append(("affinity", cpus)))
    monkeypatch.setattr(resource, "setrlimit", lambda key, value: calls.append((key, value)))
    return calls


def test_main_success_and_failure_with_mocks(tmp_path, monkeypatch, mocked_setup):
    path, digest, workspace, _ = _write_manifest(tmp_path)
    socket_dir = tmp_path / "sock"
    socket_dir.mkdir()
    os.chmod(socket_dir, 0o700)
    (socket_dir / "ipc").mkdir()
    os.chmod(socket_dir / "ipc", 0o700)
    calls = {}
    import packages.providers.gguf_worker as worker

    def _fake_build(manifest, sealed, sock, **kw):
        calls["sealed"] = dict(sealed)
        assert set(sealed) == {e["guest"] for e in manifest["assets"]}
        return ["bwrap", "/bin/bash", "-c", "exec true"]

    monkeypatch.setattr(worker, "build_args", _fake_build)
    monkeypatch.setattr("os.execve", lambda a, b, e: calls.setdefault("execve", (a, b, e)))
    monkeypatch.setattr(
        "os.sched_setaffinity", lambda pid, cpus: calls.setdefault("affinity", cpus)
    )
    monkeypatch.setattr(resource, "setrlimit", lambda r, v: calls.setdefault(f"rlimit{r}", v))
    with pytest.raises(SystemExit, match="GGUF worker startup failed"):
        worker.main(
            [
                "--manifest",
                str(path),
                "--manifest-sha256",
                digest,
                "--socket-dir",
                str(socket_dir),
                "--workspace",
                str(workspace),
            ]
        )
    assert calls["execve"][1][:1] == ["bwrap"]
    assert calls["execve"][2] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert calls["affinity"] is not None and len(calls["affinity"]) == 2
    phase = json.loads((socket_dir / "phase.json").read_bytes().decode())
    assert phase == {"version": 1, "pid": os.getpid(), "phase": "exec_pending"}
    # failure before exec records pre_exec_failed
    socket_dir2 = tmp_path / "sock2"
    socket_dir2.mkdir()
    os.chmod(socket_dir2, 0o700)
    (socket_dir2 / "ipc").mkdir()
    os.chmod(socket_dir2 / "ipc", 0o700)
    with pytest.raises(SystemExit, match="GGUF worker startup failed"):
        worker.main(
            [
                "--manifest",
                str(path),
                "--manifest-sha256",
                "0" * 64,
                "--socket-dir",
                str(socket_dir2),
                "--workspace",
                str(workspace),
            ]
        )
    phase2 = json.loads((socket_dir2 / "phase.json").read_bytes().decode())
    assert phase2["phase"] == "pre_exec_failed"


def test_main_rejects_bad_ipc_and_symlink_phase(tmp_path, mocked_setup):
    import packages.providers.gguf_worker as worker

    path, digest, workspace, _ = _write_manifest(tmp_path)
    socket_dir = tmp_path / "sock"
    socket_dir.mkdir()
    os.chmod(socket_dir, 0o700)
    ipc = socket_dir / "ipc"
    ipc.mkdir()
    os.chmod(ipc, 0o700)
    (ipc / "junk").write_bytes(b"x")
    with pytest.raises(SystemExit, match="GGUF worker startup failed"):
        worker.main(
            [
                "--manifest",
                str(path),
                "--manifest-sha256",
                digest,
                "--socket-dir",
                str(socket_dir),
                "--workspace",
                str(workspace),
            ]
        )
    (ipc / "junk").unlink()
    link = socket_dir / "phase.json"
    try:
        link.symlink_to(ipc)
    except OSError:
        pass
    else:
        with pytest.raises(SystemExit, match="GGUF worker startup failed"):
            worker.main(
                [
                    "--manifest",
                    str(path),
                    "--manifest-sha256",
                    digest,
                    "--socket-dir",
                    str(socket_dir),
                    "--workspace",
                    str(workspace),
                ]
            )


@pytest.mark.parametrize("prior", [b"foreign", b'{"version":1,"pid":1,"phase":"preparing"}'])
def test_phase_initial_never_overwrites_existing(tmp_path, prior):
    root = tmp_path / "receipt"
    root.mkdir(mode=0o700)
    receipt = root / "phase.json"
    receipt.write_bytes(prior)
    receipt.chmod(0o600)
    with pytest.raises((ValueError, FileExistsError)):
        gguf_worker.record_phase(root, "preparing")
    assert receipt.read_bytes() == prior


@pytest.mark.parametrize(
    "first,second",
    [
        ("exec_pending", "pre_exec_failed"),
        ("pre_exec_failed", "exec_pending"),
        ("exec_pending", "exec_pending"),
    ],
)
def test_phase_transition_is_terminal(tmp_path, first, second):
    root = tmp_path / "receipt"
    root.mkdir(mode=0o700)
    gguf_worker.record_phase(root, "preparing")
    gguf_worker.record_phase(root, first)
    prior = (root / "phase.json").read_bytes()
    with pytest.raises(ValueError):
        gguf_worker.record_phase(root, second)
    assert (root / "phase.json").read_bytes() == prior


@pytest.mark.parametrize(
    "field,value", [("profile", "é" * 65), ("model", "\ud800"), ("top_k", 1.5)]
)
def test_manifest_utf8_and_integer_contract(tmp_path, field, value):
    path, _, workspace, _ = _write_manifest(tmp_path)
    doc = json.loads(path.read_bytes())
    (doc["sampling"] if field == "top_k" else doc)[field] = value
    raw = json.dumps(doc).encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        gguf_assets.load_manifest(path, hashlib.sha256(raw).hexdigest(), workspace)


def test_runtime_executable_capped_at_64_mib(tmp_path):
    path, _, workspace, _ = _write_manifest(tmp_path)
    doc = json.loads(path.read_bytes())
    entry = doc["assets"][1]
    with open(entry["path"], "r+b") as stream:
        stream.truncate(64 * 1024**2 + 1)
    entry["bytes"] = 64 * 1024**2 + 1
    raw = json.dumps(doc).encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        gguf_assets.load_manifest(path, hashlib.sha256(raw).hexdigest(), workspace)


def test_private_dir_rejects_intermediate_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (real / "socket").mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(ValueError):
        gguf_worker._private_dir(link / "socket", "socket dir")
    assert len(os.listdir("/proc/self/fd")) == before


def test_build_args_rejects_unvalidated_mount_guest(tmp_path):
    path, digest, workspace, _ = _write_manifest(tmp_path)
    doc = gguf_assets.load_manifest(path, digest, workspace)
    root = tmp_path / "socket"
    root.mkdir(mode=0o700)
    (root / "ipc").mkdir(mode=0o700)
    doc["assets"][2]["guest"] = "/etc/arbitrary"
    with pytest.raises(ValueError):
        gguf_worker.build_args(doc, {entry["guest"]: 5 for entry in doc["assets"]}, root)


def test_build_args_rejects_unsealed_fds(tmp_path):
    path, digest, workspace, _ = _write_manifest(tmp_path)
    doc = gguf_assets.load_manifest(path, digest, workspace)
    root = tmp_path / "socket"
    root.mkdir(mode=0o700)
    (root / "ipc").mkdir(mode=0o700)
    with ExitStack() as stack:
        sealed = {
            entry["guest"]: stack.enter_context(open(entry["path"], "rb")).fileno()
            for entry in doc["assets"]
        }
        with pytest.raises(ValueError):
            gguf_worker.build_args(doc, sealed, root)


@pytest.fixture
def sealed_case(tmp_path):
    from packages.providers.whistle_assets import sealed_asset

    path, digest, workspace, _ = _write_manifest(tmp_path)
    manifest = gguf_assets.load_manifest(path, digest, workspace)
    root = tmp_path / "socket"
    root.mkdir(mode=0o700)
    (root / "ipc").mkdir(mode=0o700)
    with ExitStack() as stack:
        sealed = {
            e["guest"]: stack.enter_context(sealed_asset(e["path"], e, workspace))
            for e in manifest["assets"]
        }
        yield manifest, sealed, root


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.update(alias="x; env"),
        lambda m: m["assets"].append(dict(m["assets"][0])),
        lambda m: m["assets"].pop(1),
        lambda m: m["assets"][0].update(guest="/runtime/../escape"),
        lambda m: m["assets"][0].update(guest="/vendor/nested/file"),
        lambda m: m["sampling"].update(temperature=float("nan")),
        lambda m: m["sampling"].update(top_k=2.0),
        lambda m: m["sampling"].update(top_k=True),
        lambda m: m["sampling"].update(top_p=2),
        lambda m: m["sampling"].update(extra=1),
        lambda m: m["assets"][1].update(bytes=64 * 1024**2 + 1),
    ],
)
def test_build_revalidates_full_structure(sealed_case, change):
    manifest, sealed, root = sealed_case
    change(manifest)
    with pytest.raises(ValueError):
        gguf_worker.build_args(manifest, sealed, root)


@pytest.mark.parametrize(
    "values",
    [
        {"ctx_size": True},
        {"ctx_size": 32769},
        {"max_new_tokens": 15},
        {"ctx_size": 64, "max_new_tokens": 128},
        {"cpu_count": True},
    ],
)
def test_build_strict_numeric_bounds(sealed_case, values):
    manifest, sealed, root = sealed_case
    with pytest.raises(ValueError):
        gguf_worker.build_args(manifest, sealed, root, **values)


def test_build_refuses_unisolated_base(sealed_case, monkeypatch):
    import services.agent.tools as tools

    manifest, sealed, root = sealed_case
    monkeypatch.setattr(tools, "build_bwrap_args", lambda *args: (["bwrap"], False))
    with pytest.raises(ValueError, match="isolation"):
        gguf_worker.build_args(manifest, sealed, root)


@pytest.mark.parametrize(
    "bad",
    [
        {"version": True},
        {"pid": True},
        {"pid": 1},
        {"extra": 1},
        {"phase": "exec_pending"},
    ],
)
def test_phase_refuses_foreign_or_malformed_replacement(tmp_path, bad):
    root = tmp_path / "receipt"
    root.mkdir(mode=0o700)
    prior = {"version": 1, "pid": os.getpid(), "phase": "preparing"}
    prior.update(bad)
    raw = json.dumps(prior).encode()
    receipt = root / "phase.json"
    receipt.write_bytes(raw)
    receipt.chmod(0o600)
    with pytest.raises(ValueError):
        gguf_worker.record_phase(root, "pre_exec_failed")
    assert receipt.read_bytes() == raw
    assert sorted(p.name for p in root.iterdir()) == ["phase.json"]


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "permissive", "oversized", "duplicate", "utf8"]
)
def test_phase_refuses_unsafe_existing_file(tmp_path, kind):
    root = tmp_path / "receipt"
    root.mkdir(mode=0o700)
    gguf_worker.record_phase(root, "preparing")
    receipt = root / "phase.json"
    original = receipt.read_bytes()
    if kind == "symlink":
        receipt.rename(root / "other")
        receipt.symlink_to(root / "other")
    elif kind == "hardlink":
        os.link(receipt, root / "other")
    elif kind == "permissive":
        receipt.chmod(0o644)
    elif kind == "oversized":
        receipt.write_bytes(b"x" * 1025)
    elif kind == "duplicate":
        receipt.write_bytes(original[:-1] + b',"phase":"preparing"}')
    else:
        receipt.write_bytes(b"\xff")
    before = receipt.read_bytes()
    with pytest.raises((ValueError, OSError)):
        gguf_worker.record_phase(root, "exec_pending")
    assert receipt.read_bytes() == before


def test_phase_replacement_race_does_not_overwrite_foreign_file(tmp_path, monkeypatch):
    root = tmp_path / "receipt"
    root.mkdir(mode=0o700)
    gguf_worker.record_phase(root, "preparing")
    receipt = root / "phase.json"
    write = gguf_worker._write_all

    def replace_after_temp_write(fd, raw):
        write(fd, raw)
        receipt.rename(root / "original")
        receipt.write_bytes(b"foreign-race")

    monkeypatch.setattr(gguf_worker, "_write_all", replace_after_temp_write)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(ValueError):
        gguf_worker.record_phase(root, "exec_pending")
    assert receipt.read_bytes() == b"foreign-race"
    assert not list(root.glob(".phase.*"))
    assert len(os.listdir("/proc/self/fd")) == before


def test_phase_write_failure_removes_only_owned_temporary(tmp_path, monkeypatch):
    root = tmp_path / "receipt"
    root.mkdir(mode=0o700)
    gguf_worker.record_phase(root, "preparing")
    prior = (root / "phase.json").read_bytes()

    def fail(fd, raw):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(gguf_worker, "_write_all", fail)
    with pytest.raises(OSError):
        gguf_worker.record_phase(root, "exec_pending")
    assert (root / "phase.json").read_bytes() == prior
    assert not list(root.glob(".phase.*"))


def test_private_dir_refuses_inode_substitution(tmp_path, monkeypatch):
    root = tmp_path / "socket"
    root.mkdir(mode=0o700)
    opened = os.open

    def substituted(path, flags, *args, **kwargs):
        fd = opened(path, flags, *args, **kwargs)
        if Path(path) == root:
            root.rename(tmp_path / "old")
            root.mkdir(mode=0o700)
        return fd

    monkeypatch.setattr(os, "open", substituted)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(ValueError):
        gguf_worker._private_dir(root, "socket")
    assert len(os.listdir("/proc/self/fd")) == before


def _main_arguments(tmp_path):
    path, digest, workspace, _ = _write_manifest(tmp_path)
    root = tmp_path / "main-socket"
    root.mkdir(mode=0o700)
    return root, [
        "--manifest",
        str(path),
        "--manifest-sha256",
        digest,
        "--socket-dir",
        str(root),
        "--workspace",
        str(workspace),
    ]


def test_main_seals_before_fsize_and_exec_and_releases_fds(tmp_path, monkeypatch, mocked_setup):
    import fcntl

    root, args = _main_arguments(tmp_path)
    descriptors = []
    native_argv = []
    real_build = gguf_worker.build_args

    def build(manifest, sealed, directory, **kwargs):
        descriptors.extend(sealed.values())
        result = real_build(manifest, sealed, directory, **kwargs)
        native_argv.extend(result)
        return result

    def execve(executable, argv, env):
        assert executable == native_argv[0] and argv == native_argv
        assert env == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        assert json.loads((root / "phase.json").read_bytes())["phase"] == "exec_pending"
        assert mocked_setup[-1] == (resource.RLIMIT_FSIZE, (0, 0))
        assert (resource.RLIMIT_AS, (16 * 1024**3, 16 * 1024**3)) in mocked_setup
        assert (resource.RLIMIT_CORE, (0, 0)) in mocked_setup
        assert ("affinity", [2, 4]) in mocked_setup
        assert not any(call[0] == resource.RLIMIT_CPU for call in mocked_setup)
        for fd in descriptors:
            assert os.get_inheritable(fd)
            assert fcntl.fcntl(fd, fcntl.F_GET_SEALS) & fcntl.F_SEAL_WRITE
            assert os.pread(fd, 1024, 0).startswith(b"synthetic-gguf-")
        raise OSError("synthetic exec failure with private path sentinel")

    monkeypatch.setattr(gguf_worker, "build_args", build)
    monkeypatch.setattr(os, "execve", execve)
    with pytest.raises(SystemExit) as error:
        gguf_worker.main(args)
    assert str(error.value) == "GGUF worker startup failed"
    assert error.value.__suppress_context__
    assert json.loads((root / "phase.json").read_bytes())["phase"] == "exec_pending"
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_main_uncertain_pending_publication_never_downgraded(tmp_path, monkeypatch, mocked_setup):
    root, args = _main_arguments(tmp_path)
    record = gguf_worker.record_phase
    phases = []

    def uncertain(directory, phase):
        phases.append(phase)
        record(directory, phase)
        if phase == "exec_pending":
            raise OSError("synthetic fsync uncertainty")

    monkeypatch.setattr(gguf_worker, "record_phase", uncertain)
    monkeypatch.setattr(os, "execve", lambda *args: pytest.fail("no exec after failed publication"))
    with pytest.raises(SystemExit, match="GGUF worker startup failed"):
        gguf_worker.main(args)
    assert phases == ["preparing", "exec_pending"]
    assert json.loads((root / "phase.json").read_bytes())["phase"] == "exec_pending"
    assert not any(call[0] == resource.RLIMIT_FSIZE for call in mocked_setup)


def test_main_build_failure_is_pre_exec_failed_and_closes_inputs(
    tmp_path, monkeypatch, mocked_setup
):
    root, args = _main_arguments(tmp_path)
    descriptors = []

    def build(manifest, sealed, directory, **kwargs):
        descriptors.extend(sealed.values())
        raise ValueError("synthetic build failure")

    monkeypatch.setattr(gguf_worker, "build_args", build)
    monkeypatch.setattr(os, "execve", lambda *args: pytest.fail("unexpected exec"))
    with pytest.raises(SystemExit):
        gguf_worker.main(args)
    assert json.loads((root / "phase.json").read_bytes())["phase"] == "pre_exec_failed"
    assert not any(call[0] == resource.RLIMIT_FSIZE for call in mocked_setup)
    assert descriptors
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_manifest_rejects_workspace_alias_size_and_strict_bytes(tmp_path):
    path, _, workspace, _ = _write_manifest(tmp_path)
    doc = json.loads(path.read_bytes())
    in_workspace = workspace / "synthetic-input"
    in_workspace.write_bytes(b"a")
    doc["assets"][0].update(path=str(in_workspace), bytes=1)
    raw = json.dumps(doc).encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        gguf_assets.load_manifest(path, hashlib.sha256(raw).hexdigest(), workspace)
    doc["assets"][0].update(path=str(path), bytes=True)
    with pytest.raises(ValueError):
        gguf_assets.validate_manifest(doc)


def test_manifest_utf8_byte_limit_inclusive(tmp_path):
    path, _, workspace, _ = _write_manifest(tmp_path)
    doc = json.loads(path.read_bytes())
    doc["profile"] = "é" * 64
    raw = json.dumps(doc).encode()
    path.write_bytes(raw)
    assert (
        gguf_assets.load_manifest(path, hashlib.sha256(raw).hexdigest(), workspace)["profile"]
        == "é" * 64
    )


def test_phase_existing_temporary_is_not_overwritten(tmp_path):
    root = tmp_path / "receipt"
    root.mkdir(mode=0o700)
    gguf_worker.record_phase(root, "preparing")
    prior = (root / "phase.json").read_bytes()
    foreign = root / f".phase.{os.getpid()}.tmp"
    foreign.write_bytes(b"foreign-temporary")
    with pytest.raises(FileExistsError):
        gguf_worker.record_phase(root, "exec_pending")
    assert foreign.read_bytes() == b"foreign-temporary"
    assert (root / "phase.json").read_bytes() == prior


def test_phase_parent_substitution_does_not_write_replacement_dir(tmp_path, monkeypatch):
    root = tmp_path / "receipt"
    root.mkdir(mode=0o700)
    gguf_worker.record_phase(root, "preparing")
    write = gguf_worker._write_all

    def substitute(fd, raw):
        write(fd, raw)
        root.rename(tmp_path / "old-receipt")
        root.mkdir(mode=0o700)
        (root / "phase.json").write_bytes(b"foreign-parent")

    monkeypatch.setattr(gguf_worker, "_write_all", substitute)
    with pytest.raises(ValueError):
        gguf_worker.record_phase(root, "exec_pending")
    assert (root / "phase.json").read_bytes() == b"foreign-parent"
    assert not list((tmp_path / "old-receipt").glob(".phase.*"))


def test_phase_temporary_hardlink_race_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "receipt"
    root.mkdir(mode=0o700)
    gguf_worker.record_phase(root, "preparing")
    prior = (root / "phase.json").read_bytes()
    write = gguf_worker._write_all

    def hardlink_temp(fd, raw):
        write(fd, raw)
        os.link(root / f".phase.{os.getpid()}.tmp", root / "unexpected-link")

    monkeypatch.setattr(gguf_worker, "_write_all", hardlink_temp)
    with pytest.raises(ValueError):
        gguf_worker.record_phase(root, "exec_pending")
    assert (root / "phase.json").read_bytes() == prior
    assert not list(root.glob(".phase.*"))
