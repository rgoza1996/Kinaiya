"""Issue #566: real launch selection with no child processes or inference."""

import json
from types import SimpleNamespace

import pytest

from services import launcher
from services.common.topology import SubsystemTopology


@pytest.mark.parametrize(
    "agent,converse,decisions,services",
    [
        (False, False, True, ["session", "decisions"]),
        (True, False, False, ["session", "agent"]),
        (True, False, True, ["session", "agent", "decisions"]),
        (False, True, True, ["session", "decisions", "converse"]),
        (True, True, True, ["session", "agent", "decisions", "converse"]),
        # Core may remain available while setup has no active subsystem.
        (False, False, False, ["session"]),
    ],
)
def test_valid_selection_and_core_dependencies(agent, converse, decisions, services):
    config = {"distribution": dict(agent=agent, converse=converse, decisions=decisions)}
    assert launcher.host_service_names(config) == services
    with_resources = launcher.host_service_names({**config, "resources": {"enabled": True}})
    assert with_resources == [services[0], "resources", *services[1:]]


@pytest.mark.parametrize("agent", [False, True])
def test_converse_without_decisions_is_invalid(agent):
    with pytest.raises(ValueError, match="Converse requires Decisions"):
        SubsystemTopology.from_config(
            {"distribution": dict(agent=agent, converse=True, decisions=False)}
        )


@pytest.mark.parametrize("key", ["agent", "converse", "decisions"])
@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
def test_role_flags_are_strict_not_truthy(key, value):
    with pytest.raises(ValueError, match="booleans"):
        SubsystemTopology.from_config({"distribution": {key: value}})


@pytest.mark.parametrize("distribution", [None, False, [], "converse_only"])
def test_malformed_selection_is_not_silently_ignored(distribution):
    with pytest.raises(ValueError, match="object"):
        SubsystemTopology.from_config({"distribution": distribution})


def test_legacy_defaults_and_converse_only_meaning():
    assert launcher.host_service_names({}) == ["session", "agent"]
    assert SubsystemTopology.from_config({}, developer_all=True).services() == [
        "session",
        "agent",
        "decisions",
        "converse",
    ]
    assert launcher.host_service_names({"distribution": {"agent": False, "converse": True}}) == [
        "session",
        "decisions",
        "converse",
    ]


def launch_args(tmp_path, config, service):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return SimpleNamespace(
        config=path,
        data_dir=tmp_path / "private-data",
        workspace=tmp_path / "workspace",
        service=service,
        start_paused=False,
    )


@pytest.mark.parametrize("service", ["all", "host"])
def test_actual_launcher_respects_converse_only_selection(tmp_path, monkeypatch, service):
    observed = []
    monkeypatch.setattr(
        launcher,
        "load_or_create_tokens",
        lambda *_: {role: role[0] * 40 for role in ("owner", "agent", "converse", "decisions")},
    )
    monkeypatch.setattr(launcher, "supervise", observed.append)
    args = launch_args(tmp_path, {"distribution": {"agent": False, "converse": True}}, service)
    launcher.serve(args)
    assert len(observed) == 1
    assert list(observed[0]) == ["session", "decisions", "converse"]
    assert not args.data_dir.exists()


@pytest.mark.parametrize("service", ["all", "host", "converse"])
def test_invalid_topology_precedes_credentials_and_processes(tmp_path, monkeypatch, service):
    def forbidden(*args):
        pytest.fail("Invalid topology reached credentials or process creation")

    monkeypatch.setattr(launcher, "load_or_create_tokens", forbidden)
    monkeypatch.setattr(launcher, "supervise", forbidden)
    args = launch_args(tmp_path, {"distribution": {"converse": True, "decisions": False}}, service)
    before = args.config.read_bytes()
    with pytest.raises(ValueError, match="Converse requires Decisions"):
        launcher.serve(args)
    assert args.config.read_bytes() == before
    assert not args.data_dir.exists()
