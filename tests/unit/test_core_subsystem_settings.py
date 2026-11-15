"""#566/#567: one next-launch selector, exact native receipts, no activation."""

import importlib
import os
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import ValidationError

from packages.contracts.core_subsystems import (
    CoreSubsystemChange,
    CoreSubsystemSelection,
    core_subsystem_json,
)

MODES = ("core", "agent", "decisions", "agent-decisions", "converse-decisions", "all")


@pytest.fixture
def implementation():
    try:
        return importlib.import_module("services.settings.subsystems")
    except ModuleNotFoundError as error:
        if error.name != "services.settings.subsystems":
            raise
        pytest.fail("Core saved-mode authority is not implemented")


@pytest.fixture
def owner(tmp_path, implementation):
    current = ["agent"]
    instance = implementation.CoreSubsystemSettings(
        lambda: current[0],
        path=tmp_path / "private/core-subsystems.sqlite",
        workspace=tmp_path / "workspace",
        startup_mode="agent",
    )
    yield instance, current
    instance.close()


def request(instance, mode="all", *, key="public-mode-change", source="saved"):
    snapshot = instance.snapshot()
    return CoreSubsystemChange(
        backend_id=snapshot["backend_id"],
        authority_epoch=snapshot["authority_epoch"],
        authority_generation=snapshot["authority_generation"],
        expected_revision=snapshot["revision"],
        state_token=snapshot["state_token"],
        expected_host_mode=snapshot["host_mode"],
        idempotency_key=key,
        selection=CoreSubsystemSelection(source=source, mode=mode),
    )


@pytest.mark.parametrize("mode", MODES)
def test_save_is_exact_and_next_launch_only(owner, mode, monkeypatch):
    instance, _ = owner
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: pytest.fail("mode save started a process")
    )
    monkeypatch.setattr(os, "system", lambda *a, **k: pytest.fail("mode save ran a command"))
    before = instance.snapshot()
    original = request(instance, mode)
    receipt = instance.change(original)
    after = instance.snapshot()
    assert receipt["status"] == "saved" and receipt["revision"] == before["revision"] + 1
    assert receipt["selection"] == {"source": "saved", "mode": mode}
    assert after["selected_mode"] == mode
    assert after["startup_mode"] == "agent"
    assert after["running_state"] == "not_observed"
    assert after["change_boundary"] == receipt["change_boundary"] == "next_managed_host_launch"
    assert instance.change(original) == receipt
    assert instance.lookup(original)["receipt"] == receipt
    assert instance.snapshot() == after


def test_host_source_remains_a_pointer_and_receipt_is_historical(owner):
    instance, current = owner
    receipt = instance.change(request(instance, None, source="host"))
    current[0] = "converse-decisions"
    snapshot = instance.snapshot()
    assert snapshot["selection"] == {"source": "host", "mode": None}
    assert snapshot["selected_mode"] == "converse-decisions"
    assert snapshot["startup_mode"] == "agent"
    assert receipt["host_mode_observed"] == receipt["selected_mode_observed"] == "agent"


def test_host_change_fences_save_but_is_not_a_durable_lookup_fence(owner, implementation):
    instance, current = owner
    original = request(instance)
    current[0] = "decisions"
    with pytest.raises(implementation.CoreSubsystemConflict):
        instance.change(original)
    lookup = instance.lookup(original)
    assert lookup["outcome"] == "unobserved" and lookup["mutation_fenced"] is False
    current[0] = "agent"
    assert instance.change(original)["status"] == "saved"


def test_new_revision_is_a_durable_fence_for_an_unobserved_original(owner):
    instance, _ = owner
    original = request(instance, "core", key="old")
    instance.change(request(instance, "decisions", key="new"))
    proof = instance.lookup(original)
    assert proof["outcome"] == "unobserved" and proof["mutation_fenced"] is True


def test_same_key_different_original_refuses_without_mutating(owner, implementation):
    instance, _ = owner
    original = request(instance)
    receipt = instance.change(original)
    changed = original.model_copy(
        update={"selection": CoreSubsystemSelection(source="saved", mode="core")}
    )
    with pytest.raises(implementation.CoreSubsystemConflict):
        instance.change(changed)
    assert instance.change(original) == receipt
    assert instance.snapshot()["revision"] == 1


def test_native_cli_peer_shares_epoch_without_retiring_session(owner, tmp_path, implementation):
    instance, current = owner
    before = instance.snapshot()
    peer = implementation.CoreSubsystemSettings(
        lambda: current[0],
        path=tmp_path / "private/core-subsystems.sqlite",
        workspace=tmp_path / "workspace",
        register=False,
    )
    try:
        assert peer.snapshot()["authority_epoch"] == before["authority_epoch"]
        receipt = peer.change(request(peer, "decisions"))
        after = instance.snapshot()
        assert after["authority_epoch"] == before["authority_epoch"]
        assert after["revision"] == receipt["revision"] == 1
        assert after["startup_mode"] == "agent" and after["selected_mode"] == "decisions"
    finally:
        peer.close()


def test_registered_replacement_retires_old_writes_and_keeps_exact_receipts(
    owner, tmp_path, implementation
):
    instance, current = owner
    committed = request(instance, "decisions", key="committed")
    receipt = instance.change(committed)
    unobserved = request(instance, "all", key="unobserved")
    replacement = implementation.CoreSubsystemSettings(
        lambda: current[0],
        path=tmp_path / "private/core-subsystems.sqlite",
        workspace=tmp_path / "workspace",
        startup_mode="decisions",
    )
    try:
        with pytest.raises(implementation.CoreSubsystemUnavailable):
            instance.snapshot()
        with pytest.raises(implementation.CoreSubsystemConflict):
            instance.change(unobserved)
        assert instance.change(committed) == replacement.change(committed) == receipt
        assert replacement.lookup(unobserved)["mutation_fenced"] is True
        assert instance.lookup(unobserved)["mutation_fenced"] is True
    finally:
        replacement.close()


def test_future_revision_or_generation_never_gets_a_false_mutation_fence(owner):
    instance, _ = owner
    original = request(instance)
    for field in ("expected_revision", "authority_generation"):
        future = original.model_copy(update={field: 100})
        assert instance.lookup(future)["mutation_fenced"] is False


def test_invalid_host_default_stays_unknown_and_saved_mode_can_repair_selection(
    owner, implementation
):
    instance, current = owner
    current[0] = None
    snapshot = instance.snapshot()
    assert snapshot["host_mode"] is None and snapshot["selected_mode"] is None
    with pytest.raises(implementation.CoreSubsystemConflict):
        instance.change(request(instance, None, source="host"))
    receipt = instance.change(request(instance, "converse-decisions", key="repair"))
    assert receipt["selected_mode_observed"] == "converse-decisions"
    assert instance.snapshot()["host_mode"] is None


def test_two_native_writers_cannot_both_commit_the_same_revision(owner, tmp_path, implementation):
    instance, current = owner
    barrier = Barrier(2)

    def save(mode):
        peer = implementation.CoreSubsystemSettings(
            lambda: current[0],
            path=tmp_path / "private/core-subsystems.sqlite",
            workspace=tmp_path / "workspace",
            register=False,
        )
        try:
            original = request(peer, mode, key=mode)
            barrier.wait(timeout=5)
            try:
                return peer.change(original)["revision"]
            except implementation.CoreSubsystemConflict:
                return "conflict"
        finally:
            peer.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, ("core", "all")))
    assert sorted(results, key=str) == [1, "conflict"]
    assert instance.snapshot()["revision"] == 1


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("core_subsystem_state", "selection", '{"source":"saved","mode":"converse"}'),
        ("core_subsystem_state", "revision", 0.5),
        ("core_subsystem_state", "backend_id", "wrong"),
    ],
)
def test_corrupt_state_refuses_instead_of_defaulting(owner, table, column, value, implementation):
    instance, _ = owner
    instance.db.execute(f"UPDATE {table} SET {column}=?", (value,))
    instance.db.commit()
    with pytest.raises(implementation.CoreSubsystemUnavailable):
        instance.snapshot()


def test_existing_empty_or_unknown_store_is_preserved(tmp_path, implementation):
    workspace = tmp_path / "workspace"
    path = tmp_path / "private/core-subsystems.sqlite"
    path.parent.mkdir(mode=0o700)
    path.touch(mode=0o600)
    before = path.read_bytes()
    with pytest.raises(implementation.CoreSubsystemUnavailable):
        implementation.CoreSubsystemSettings(lambda: "agent", path=path, workspace=workspace)
    assert path.read_bytes() == before
    path.unlink()
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE foreign_state(x)")
    db.commit()
    db.close()
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(implementation.CoreSubsystemUnavailable):
        implementation.CoreSubsystemSettings(lambda: "agent", path=path, workspace=workspace)
    assert path.read_bytes() == before


def test_replaced_storage_identity_cannot_be_adopted(owner, tmp_path, implementation):
    instance, _ = owner
    path = tmp_path / "private/core-subsystems.sqlite"
    path.rename(path.with_suffix(".original"))
    path.touch(mode=0o600)
    with pytest.raises(implementation.CoreSubsystemUnavailable):
        instance.snapshot()


@pytest.mark.parametrize("kind", ["alias", "workspace", "public", "hardlink"])
def test_storage_guards_reject_untrusted_locations(tmp_path, implementation, kind):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    path = private / "core-subsystems.sqlite"
    if kind == "alias":
        path.symlink_to(private / "missing")
    elif kind == "workspace":
        path = workspace / "core-subsystems.sqlite"
    elif kind == "public":
        path.touch(mode=0o644)
    else:
        path.touch(mode=0o600)
        os.link(path, private / "other")
    with pytest.raises((implementation.CoreSubsystemUnavailable, ValueError, OSError)):
        implementation.CoreSubsystemSettings(lambda: "agent", path=path, workspace=workspace)


@pytest.mark.parametrize("after_commit", [False, True])
def test_uncertain_commit_reconciles_original_without_replacing_it(
    owner, tmp_path, implementation, after_commit
):
    instance, current = owner
    original = request(instance)
    native = instance.db

    class BrokenCommit:
        def __getattr__(self, name):
            return getattr(native, name)

        def commit(self):
            if after_commit:
                native.commit()
            raise OSError("synthetic interrupted commit")

    instance.db = BrokenCommit()
    with pytest.raises(implementation.CoreSubsystemUnavailable):
        instance.change(original)
    instance.close()
    replacement = implementation.CoreSubsystemSettings(
        lambda: current[0],
        path=tmp_path / "private/core-subsystems.sqlite",
        workspace=tmp_path / "workspace",
    )
    try:
        proof = replacement.lookup(original)
        if after_commit:
            assert proof["outcome"] == "saved"
            assert proof["receipt"]["idempotency_key"] == original.idempotency_key
            assert replacement.change(original) == proof["receipt"]
        else:
            assert proof["outcome"] == "unobserved" and proof["mutation_fenced"] is True
        assert replacement.snapshot()["revision"] == int(after_commit)
    finally:
        replacement.close()


@pytest.mark.parametrize("mode", ["converse", "converse-agent", "", True, 1, {}, []])
def test_unsupported_or_invalid_modes_are_not_selectable(mode):
    with pytest.raises(ValidationError):
        CoreSubsystemSelection(source="saved", mode=mode)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":1.0}',
        b"[]",
        b'{"a":' + b"[" * 30 + b"0" + b"]" * 30 + b"}",
        b'{"a":"' + b"x" * 4096 + b'"}',
    ],
)
def test_wire_json_bounds_and_ambiguity_refuse(raw):
    with pytest.raises(ValueError):
        core_subsystem_json(raw)
