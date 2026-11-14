"""Native config wiring with fake components only; no client, decoder, or services."""

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.agent import task_media_launcher as module

SHA = "a" * 64


@pytest.fixture
def setup(tmp_path, monkeypatch):
    data, workspace = tmp_path / "owner", tmp_path / "workspace"
    data.mkdir(mode=0o700)
    workspace.mkdir()
    calls = []

    class Assets:
        def __init__(self, path):
            self.path, self.closed = path, False
            calls.append(("assets", path))

        def close(self):
            self.closed = True
            calls.append(("assets_close",))

    class Provider:
        def __init__(self, **kwargs):
            self.kwargs, self.calls, self.close_error = kwargs, [], None

        async def __call__(self, prepared, current):
            current()
            self.calls.append(prepared)
            return "synthetic observed provider result"

        async def close(self):
            calls.append(("provider_close",))
            if self.close_error is not None:
                raise self.close_error

    class Factory:
        def __init__(self, *, plan_path, provider):
            self.plan_path, self.provider = plan_path, provider

        async def enroll(self, *_):
            return True

    monkeypatch.setattr(
        module,
        "_components",
        lambda: (
            Assets,
            Provider,
            lambda **values: SimpleNamespace(**values),
            lambda **values: SimpleNamespace(**values),
            Factory,
        ),
    )
    monkeypatch.setenv("SYNTHETIC_MEDIA_KEY", "fixture-only-not-a-credential")
    declaration = {
        "kind": "meta_video_v1",
        "owner_plan": str(data / "later-plan.json"),
        "api_key_env": "SYNTHETIC_MEDIA_KEY",
        "video_profile": {
            "source_sha256": SHA,
            "width": 1280,
            "height": 720,
            "fps_num": 30000,
            "fps_den": 1001,
            "frame_count": 1273,
            "duration_seconds": 42.631837,
        },
    }
    return SimpleNamespace(data=data, workspace=workspace, declaration=declaration, calls=calls)


def create(setup):
    return module.task_media_owner(
        {"task_media": setup.declaration}, data_dir=setup.data, workspace=setup.workspace
    )


def test_default_host_does_not_import_components_or_create_owner(monkeypatch, tmp_path):
    def forbidden():
        pytest.fail("Default host imported optional media components")

    monkeypatch.setattr(module, "_components", forbidden)
    assert module.task_media_owner({}, data_dir=tmp_path, workspace=tmp_path) is None


async def test_native_owner_uses_one_registry_and_exact_preworker_factory(setup):
    owner = create(setup)
    assert owner.media_assets.path == setup.data / "media-assets.sqlite"
    assert owner.factory.plan_path == Path(setup.declaration["owner_plan"])
    assert not owner.factory.plan_path.exists()  # Native clip receipt arrives later.
    projector = owner.adapter.kwargs["sam_projector"]
    assert projector.media_assets is owner.media_assets
    assert projector.work_root == setup.data / "sam-render"
    assert projector.profile.source_sha256 == SHA
    assert owner.adapter.kwargs["api_key"] == "fixture-only-not-a-credential"
    runtime = SimpleNamespace(task_harness_broker_factory=None, task_preworker_hook=None)
    owner.bind(runtime)
    assert runtime.task_harness_broker_factory is owner.factory
    assert runtime.task_preworker_hook == owner.factory.enroll
    assert runtime.task_media_owner is owner
    await owner.close()
    assert setup.calls[-2:] == [("provider_close",), ("assets_close",)]


@pytest.mark.parametrize("field", ["task_harness_broker_factory", "task_preworker_hook"])
def test_native_owner_cannot_replace_another_owner(setup, field):
    owner = create(setup)
    runtime = SimpleNamespace(task_harness_broker_factory=None, task_preworker_hook=None)
    original = object()
    setattr(runtime, field, original)
    with pytest.raises(ValueError, match="already configured"):
        owner.bind(runtime)
    assert getattr(runtime, field) is original


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "other"},
        {"extra": True},
        {"api_key": "must-never-be-accepted"},
        {"owner_plan": "relative.json"},
        {"owner_plan": 3},
        {"api_key_env": "BAD-NAME"},
        {"api_key_env": "é"},
        {"api_key_env": ""},
        {"api_key_env": "UNSET_SYNTHETIC_MEDIA_REFERENCE"},
        {"video_profile": []},
    ],
)
def test_invalid_declaration_refuses_before_component_construction(setup, change):
    setup.declaration.update(change)
    with pytest.raises(ValueError, match="Native task media configuration is unavailable"):
        create(setup)
    assert setup.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"source_sha256": "A" * 64},
        {"source_sha256": "short"},
        {"source_sha256": None},
        {"width": True},
        {"width": 1281},
        {"height": 721},
        {"height": 0},
        {"fps_num": False},
        {"fps_num": 120_001},
        {"fps_den": 0},
        {"fps_den": 10_001},
        {"fps_num": 1, "fps_den": 2},
        {"fps_num": 61, "fps_den": 1},
        {"frame_count": 1801},
        {"frame_count": 1.0},
        {"duration_seconds": True},
        {"duration_seconds": float("inf")},
        {"duration_seconds": float("nan")},
        {"duration_seconds": 60.001},
        {"duration_seconds": 0},
        {"prompt": "no new authority"},
    ],
)
def test_source_profile_is_strict_native_bounded_metadata(setup, change):
    setup.declaration["video_profile"].update(change)
    with pytest.raises(ValueError, match="Native task media configuration is unavailable"):
        create(setup)
    assert setup.calls == []


@pytest.mark.parametrize("field", ["owner_plan", "data_dir"])
def test_native_owner_storage_must_remain_outside_workspace(setup, field):
    if field == "owner_plan":
        setup.declaration[field] = str(setup.workspace / "plan.json")
    else:
        setup.data = setup.workspace
    with pytest.raises(ValueError, match="Native task media configuration is unavailable"):
        create(setup)
    assert setup.calls == []


def test_owner_plan_symlink_is_refused_before_components(setup):
    target = setup.data / "target.json"
    target.write_text("synthetic")
    Path(setup.declaration["owner_plan"]).symlink_to(target)
    with pytest.raises(ValueError, match="Native task media configuration is unavailable"):
        create(setup)
    assert setup.calls == []


async def test_profile_source_mismatch_never_dispatches_the_provider(setup):
    owner = create(setup)
    prepared = SimpleNamespace(grant=SimpleNamespace(source_sha256="b" * 64))
    checks = []
    with pytest.raises(ValueError, match="differs from the native rendering profile"):
        await owner.factory.provider(prepared, lambda: checks.append(True))
    assert checks == [True] and owner.adapter.calls == []
    prepared.grant.source_sha256 = SHA
    assert (
        await owner.factory.provider(prepared, lambda: None) == "synthetic observed provider result"
    )
    assert owner.adapter.calls == [prepared]


async def test_provider_close_failure_retains_shared_registry_and_original_owner(setup):
    owner = create(setup)
    original = RuntimeError("synthetic original cleanup failure")
    owner.adapter.close_error = original
    with pytest.raises(RuntimeError) as observed:
        await owner.close()
    assert observed.value is original
    assert not owner.media_assets.closed
    assert setup.calls[-1] == ("provider_close",)


def test_configuration_input_is_not_mutated(setup):
    original = copy.deepcopy(setup.declaration)
    create(setup)
    assert setup.declaration == original
