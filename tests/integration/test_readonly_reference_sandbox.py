"""Actual BWrap proofs for readable references and immutable mount paths."""

import asyncio
import os
import shlex

import pytest

from core.policy import ToolPolicy
from services.agent.tools import ToolFailure, run_read, run_search, run_shell, run_write


@pytest.mark.sandbox
async def test_readonly_references_survive_shell_alias_rename_and_remount_attacks(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/spec.txt").write_text("public test contract")
    (tmp_path / "README.md").write_text("public README")
    nested = tmp_path / "parent/deeper"
    nested.mkdir(parents=True)
    (nested / "reference.txt").write_text("nested reference")
    (nested / "core").mkdir()
    (nested / "core/guard").write_text("protected core fixture")
    policy = ToolPolicy(
        tmp_path,
        yolo=True,
        protected_roots=[nested / "core"],
        readonly_roots=[tmp_path / "README.md", tmp_path / "tests", nested / "reference.txt"],
    )
    assert run_read(policy, "README.md", 65536, 1)[0] == "public README"
    assert "tests/spec.txt" in run_search(policy, "contract", None, 1)[0]
    run_write(policy, "source.py", "permitted source", 1)
    try:
        await run_shell(policy, "true", 5, 1)
    except ToolFailure as exc:
        pytest.skip(f"OS sandbox unavailable: {exc}")
    script = """import os, subprocess, socket
from pathlib import Path
references = {"README.md": "public README", "tests/spec.txt": "public test contract", "parent/deeper/reference.txt": "nested reference", "parent/deeper/core/guard": "protected core fixture"}
def denied(operation):
    try:
        operation()
    except OSError:
        return
    raise AssertionError("reference mutation succeeded")
for name, text in references.items():
    assert Path(name).read_text() == text
    denied(lambda name=name: Path(name).write_text("bad"))
    denied(lambda name=name: os.unlink(name))
    Path("replacement").write_text("bad")
    denied(lambda name=name: os.replace("replacement", name))
    Path("replacement").unlink()
    denied(lambda name=name: os.link(name, "hard-alias"))
    Path("symbolic-alias").symlink_to(name)
    denied(lambda: Path("symbolic-alias").write_text("bad"))
    Path("symbolic-alias").unlink()
for parent in ("tests", "parent", "parent/deeper"):
    denied(lambda parent=parent: os.rename(parent, "renamed-parent"))
for name in references:
    result = subprocess.run(["mount", "-o", "remount,rw", "/workspace/" + name], capture_output=True)
    assert result.returncode != 0
nested_namespace = subprocess.run(["unshare", "--user", "--map-root-user", "--mount",
    "mount", "-o", "remount,rw", "/workspace/tests"], capture_output=True)
assert nested_namespace.returncode != 0
# BWrap consumes source FDs; they cannot become a host-path bypass.
assert all(int(fd) <= 3 for fd in os.listdir("/proc/self/fd"))
assert all(name == "lo" for _, name in socket.if_nameindex())
Path("source.py").write_text("ordinary source remains writable")
for name, text in references.items():
    assert Path(name).read_text() == text
print("READONLY_REFERENCE_PROOF_OK")
"""
    _, evidence = await run_shell(policy, "python3 -B -c " + shlex.quote(script), 15, 1)
    assert evidence.exit_code == 0
    assert "READONLY_REFERENCE_PROOF_OK" in evidence.output
    assert (tmp_path / "README.md").read_text() == "public README"
    assert (tmp_path / "tests/spec.txt").read_text() == "public test contract"
    assert (nested / "reference.txt").read_text() == "nested reference"
    assert (tmp_path / "source.py").read_text() == "ordinary source remains writable"


@pytest.mark.sandbox
async def test_readonly_spawn_failure_and_cancellation_release_mount_fds(tmp_path, monkeypatch):
    reference = tmp_path / "reference.txt"
    reference.write_text("public reference")
    policy = ToolPolicy(tmp_path, allow_shell=True, readonly_roots=[reference])
    original = asyncio.create_subprocess_exec
    seen = []

    async def fail(*args, **kwargs):
        seen.extend(kwargs["pass_fds"])
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    with pytest.raises(OSError, match="synthetic spawn failure"):
        await run_shell(policy, "true", 5, 1)
    assert seen
    for descriptor in seen:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", original)
    slot = {}
    operation = asyncio.create_task(run_shell(policy, "sleep 30", 60, 1, proc_slot=slot))
    async with asyncio.timeout(5):
        while slot.get("proc") is None and not operation.done():
            await asyncio.sleep(0.01)
    handle = slot.get("proc")
    if handle is None:
        try:
            operation.result()
        except ToolFailure as exc:
            pytest.skip(f"OS sandbox unavailable: {exc}")
    assert handle is not None
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, 10)
    assert handle._proc.returncode is not None and not slot.get("proc")
    assert reference.read_text() == "public reference"
