"""#623: native preference persistence, exact recovery and fail-closed authority."""

import json
import os
import sqlite3

import pytest

from packages.contracts.capture_modes import CaptureModeChange, CaptureModeSelection
from services.converse.capture_settings import (
    CaptureModeConflict,
    CaptureModeSettings,
    CaptureModeUnavailable,
)


def change(snapshot, mode="rolling_windows", *, key="public-capture-choice"):
    return CaptureModeChange(
        backend_id=snapshot["backend_id"],
        authority_generation=snapshot["authority_generation"],
        authority_epoch=snapshot["authority_epoch"],
        expected_revision=snapshot["revision"],
        expected_default_mode=snapshot["default_mode"],
        state_token=snapshot["state_token"],
        idempotency_key=key,
        selection=CaptureModeSelection(source="saved", mode=mode),
    )


@pytest.fixture
def native(tmp_path):
    current = {"mode": "rolling_windows"}
    owners = []

    def open_owner():
        owner = CaptureModeSettings(
            lambda: current["mode"],
            path=tmp_path / "private/converse-capture-mode.sqlite",
            workspace=tmp_path / "workspace",
        )
        owners.append(owner)
        return owner

    yield open_owner, current
    for owner in reversed(owners):
        owner.close()


@pytest.mark.parametrize("mode", ["adaptive_live", "rolling_windows", "legacy_silence"])
def test_saved_choice_survives_restart_without_fallback_or_gate_activation(native, mode):
    open_owner, defaults = native
    owner = open_owner()
    original = change(owner.snapshot(), mode)
    receipt = owner.change(original)
    assert receipt["selected_mode_observed"] == mode and receipt["revision"] == 1
    assert receipt["change_boundary"] == "next_capture"
    assert owner.change(original) == receipt
    owner.close()
    defaults["mode"] = "legacy_silence" if mode != "legacy_silence" else "adaptive_live"
    restored = open_owner()
    snapshot = restored.snapshot()
    assert snapshot["selected_mode"] == mode and snapshot["selection"]["source"] == "saved"
    assert snapshot["revision"] == 1 and snapshot["adaptive_live_enabled"] is False
    assert snapshot["capture_state"] == "not_observed"
    observed = restored.lookup(original)
    assert observed["outcome"] == "saved" and observed["receipt"] == receipt
    assert observed["mutation_fenced"] is False


def test_host_default_pointer_and_explicit_choice_are_distinct(native):
    open_owner, defaults = native
    owner = open_owner()
    initial = owner.snapshot()
    assert initial["selection"] == {"source": "host", "mode": None}
    defaults["mode"] = "legacy_silence"
    assert owner.snapshot()["selected_mode"] == "legacy_silence"
    with pytest.raises(CaptureModeConflict):
        owner.change(change(initial))
    assert owner.snapshot()["revision"] == 0
    owner.change(change(owner.snapshot(), "rolling_windows"))
    defaults["mode"] = "adaptive_live"
    assert owner.snapshot()["selected_mode"] == "rolling_windows"


def test_unknown_host_default_stays_unknown_and_saved_choice_can_repair(native):
    open_owner, defaults = native
    defaults["mode"] = "unknown"
    owner = open_owner()
    initial = owner.snapshot()
    assert initial["default_mode"] is None and initial["selected_mode"] is None
    owner.change(change(initial, "legacy_silence"))
    assert owner.snapshot()["selected_mode"] == "legacy_silence"
    reset = change(owner.snapshot(), key="host-reset").model_copy(
        update={"selection": CaptureModeSelection(source="host")}
    )
    with pytest.raises(CaptureModeConflict):
        owner.change(reset)


def test_replacement_controller_fences_unobserved_original_without_replaying(native):
    open_owner, _ = native
    first = open_owner()
    original = change(first.snapshot())
    replacement = open_owner()
    with pytest.raises(CaptureModeConflict):
        first.change(original)
    with pytest.raises(CaptureModeUnavailable):
        first.snapshot()
    lookup = replacement.lookup(original)
    assert lookup["outcome"] == "unobserved" and lookup["mutation_fenced"] is True
    assert replacement.snapshot()["revision"] == 0


def test_unobserved_absence_and_default_change_alone_do_not_prove_fencing(native):
    open_owner, defaults = native
    owner = open_owner()
    original = change(owner.snapshot())
    defaults["mode"] = "legacy_silence"
    lookup = owner.lookup(original)
    assert lookup["outcome"] == "unobserved" and lookup["mutation_fenced"] is False
    defaults["mode"] = "rolling_windows"
    assert owner.change(original)["revision"] == 1


@pytest.mark.parametrize("commit_first", [False, True])
def test_lost_commit_ack_never_retries_and_new_controller_reads_native_truth(native, commit_first):
    open_owner, _ = native
    owner = open_owner()
    original = change(owner.snapshot(), "legacy_silence")
    db = owner.db

    class LostCommitAck:
        def __getattr__(self, name):
            return getattr(db, name)

        def commit(self):
            if commit_first:
                db.commit()
            raise sqlite3.OperationalError("PUBLIC-FAULT")

    owner.db = LostCommitAck()
    with pytest.raises(CaptureModeUnavailable):
        owner.change(original)
    assert owner.failed
    with pytest.raises(CaptureModeUnavailable):
        owner.change(original)
    owner.close()
    replacement = open_owner()
    lookup = replacement.lookup(original)
    assert lookup["outcome"] == ("saved" if commit_first else "unobserved")
    assert lookup["mutation_fenced"] is (not commit_first)
    assert replacement.snapshot()["revision"] == int(commit_first)
    assert replacement.db.execute("SELECT COUNT(*) FROM capture_mode_operations").fetchone() == (
        int(commit_first),
    )


def test_changed_body_under_same_key_and_stale_revision_refuse(native):
    open_owner, _ = native
    owner = open_owner()
    initial = owner.snapshot()
    original = change(initial)
    owner.change(original)
    with pytest.raises(CaptureModeConflict):
        owner.change(change(initial, "legacy_silence"))
    with pytest.raises(CaptureModeConflict):
        owner.change(change(initial, "legacy_silence", key="different-original"))
    assert owner.snapshot()["revision"] == 1


@pytest.mark.parametrize("damage", ["selection", "receipt", "extra_schema", "missing_history"])
def test_corrupt_authority_does_not_reset_or_sanitize_its_original(native, damage):
    open_owner, _ = native
    owner = open_owner()
    owner.change(change(owner.snapshot()))
    if damage == "selection":
        owner.db.execute("UPDATE capture_mode_state SET selection=?", ('{"source":"saved"}',))
    elif damage == "receipt":
        owner.db.execute("UPDATE capture_mode_operations SET receipt=?", ('{"forged":true}',))
    elif damage == "extra_schema":
        owner.db.execute("CREATE TABLE unrelated (secret TEXT)")
    else:
        owner.db.execute("DELETE FROM capture_mode_operations")
    owner.db.commit()
    with pytest.raises(CaptureModeUnavailable):
        owner.snapshot()
    assert owner.db.execute("SELECT revision FROM capture_mode_state").fetchone() == (1,)
    owner.close()
    with pytest.raises((CaptureModeUnavailable, ValueError)):
        open_owner()


def test_existing_empty_database_is_not_a_new_configuration(tmp_path):
    path = tmp_path / "empty.sqlite"
    path.touch(mode=0o600)
    with pytest.raises(CaptureModeUnavailable):
        CaptureModeSettings(lambda: "rolling_windows", path=path, workspace=tmp_path / "workspace")
    assert path.read_bytes() == b""


@pytest.mark.parametrize("kind", ["workspace", "symlink", "hardlink", "public"])
def test_unsafe_storage_refuses_without_creating_capture_state(tmp_path, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "target.sqlite"
    target.touch(mode=0o600)
    path = tmp_path / "capture.sqlite"
    if kind == "workspace":
        path = workspace / "capture.sqlite"
    elif kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        path.touch(mode=0o644)
    with pytest.raises(ValueError):
        CaptureModeSettings(lambda: "rolling_windows", path=path, workspace=workspace)
    assert target.read_bytes() == b""
    if kind == "workspace":
        assert not path.exists()


def test_replaced_path_is_not_adopted_by_existing_authority(native):
    open_owner, _ = native
    owner = open_owner()
    original = change(owner.snapshot())
    owner.path.rename(owner.path.with_suffix(".original"))
    owner.path.touch(mode=0o600)
    with pytest.raises(CaptureModeUnavailable):
        owner.change(original)
    assert owner.path.read_bytes() == b""


def test_foreign_backend_cannot_settle_original(native, tmp_path):
    open_owner, _ = native
    owner = open_owner()
    original = change(owner.snapshot())
    foreign = CaptureModeSettings(
        lambda: "rolling_windows",
        path=tmp_path / "other/capture.sqlite",
        workspace=tmp_path / "workspace",
    )
    try:
        with pytest.raises(CaptureModeConflict):
            foreign.lookup(original)
        with pytest.raises(CaptureModeConflict):
            foreign.change(original)
    finally:
        foreign.close()


@pytest.mark.parametrize("field", ["expected_revision", "authority_generation"])
def test_boolean_is_not_a_native_integer(native, field):
    open_owner, _ = native
    owner = open_owner()
    original = change(owner.snapshot()).model_dump(mode="json")
    original[field] = True
    with pytest.raises(ValueError):
        CaptureModeChange.model_validate(original)
    assert owner.snapshot()["revision"] == 0


def test_historical_receipt_contains_no_provider_or_secret_configuration(native):
    open_owner, _ = native
    owner = open_owner()
    receipt = owner.change(change(owner.snapshot()))
    assert set(receipt) == {
        "contract",
        "status",
        "backend_id",
        "authority_epoch",
        "authority_generation",
        "revision",
        "idempotency_key",
        "selection",
        "default_mode_observed",
        "selected_mode_observed",
        "change_boundary",
    }
    assert len(json.dumps(receipt).encode()) < 4096
