"""#623: qualified rollout changes implicit defaults, never explicit choices."""

import pytest

from packages.contracts.capture_modes import CaptureModeSelection
from services.converse.capture_settings import CaptureModeSettings, configured_capture_default
from tests.unit.test_capture_mode_settings import change


@pytest.mark.parametrize("saved", [None, "rolling_windows", "legacy_silence"])
def test_rollout_preserves_explicit_choice_and_moves_only_the_host_pointer(tmp_path, saved):
    path = tmp_path / "private/capture.sqlite"
    workspace = tmp_path / "workspace"
    before = CaptureModeSettings(
        lambda: configured_capture_default({}, rolling=True, native=False),
        path=path,
        workspace=workspace,
    )
    if saved is not None:
        before.change(change(before.snapshot(), saved))
    old = before.snapshot()
    before.close()
    after = CaptureModeSettings(
        lambda: configured_capture_default(
            {}, rolling=True, native=False, adaptive_live_enabled=True
        ),
        path=path,
        workspace=workspace,
        adaptive_live_enabled=True,
    )
    try:
        current = after.snapshot()
        assert old["adaptive_live_enabled"] is False
        assert current["adaptive_live_enabled"] is True
        assert current["selected_mode"] == (saved or "adaptive_live")
        assert current["selection"] == (
            CaptureModeSelection(source="host").model_dump()
            if saved is None
            else CaptureModeSelection(source="saved", mode=saved).model_dump()
        )
        assert current["revision"] == old["revision"]
        assert current["capture_state"] == "not_observed"
        assert current["change_boundary"] == "next_capture"
    finally:
        after.close()


@pytest.mark.parametrize("mode", ["rolling_windows", "legacy_silence", "adaptive_live"])
def test_explicit_host_choice_survives_qualified_default_resolution(mode):
    assert (
        configured_capture_default(
            {"converse_capture": {"mode": mode}},
            rolling=True,
            native=False,
            adaptive_live_enabled=True,
        )
        == mode
    )


@pytest.mark.parametrize("rolling", [False, True])
@pytest.mark.parametrize("native", [False, True])
def test_implicit_rollout_default_is_independent_of_provider_interface(rolling, native):
    assert (
        configured_capture_default({}, rolling=rolling, native=native, adaptive_live_enabled=True)
        == "adaptive_live"
    )
    assert configured_capture_default({}, rolling=rolling, native=native) == (
        "legacy_silence" if native or not rolling else "rolling_windows"
    )


@pytest.mark.parametrize("value", [None, 0, 1, "true", {}])
def test_rollout_requires_a_trusted_exact_boolean(tmp_path, value):
    with pytest.raises(ValueError):
        configured_capture_default({}, rolling=True, native=False, adaptive_live_enabled=value)
    with pytest.raises(ValueError):
        CaptureModeSettings(
            lambda: "rolling_windows",
            path=tmp_path / "private/capture.sqlite",
            workspace=tmp_path / "workspace",
            adaptive_live_enabled=value,
        )
