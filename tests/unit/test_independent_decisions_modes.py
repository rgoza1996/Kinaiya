"""#619: selected Decisions is independent of Converse in official setup/start."""

import json

import pytest

from services import launcher
from services.distribution import doctor, installation, start_supervisor


def config_for(agent, converse, decisions):
    return {
        "distribution": {"agent": agent, "converse": converse, "decisions": decisions},
        "decisions_port": 28763,
    }


@pytest.mark.parametrize("agent", [False, True])
def test_explicit_decisions_profile_enables_independent_setup(tmp_path, monkeypatch, agent):
    # Control only the host-prerequisite observation. Actual native init and
    # atomic setup writes stay inside the isolated layout; no package install.
    monkeypatch.setattr(installation, "check_outdated_system_blockers", lambda **_: None)
    layout = installation.Layout.for_home(tmp_path)
    installation.setup(
        layout,
        agent=agent,
        endpoint="http://127.0.0.1:1234",
        model="synthetic-agent",
        decisions_profile={
            "kind": "compatible",
            "endpoint": "http://127.0.0.1:1235",
            "model": "synthetic-independent-decisions",
        },
    )
    config = json.loads(layout.config.read_text())
    assert config["providers"]["decisions"]["model"] == "synthetic-independent-decisions"
    assert config["distribution"].get("decisions") is True
    assert launcher.host_service_names(config) == [
        "session",
        *(["agent"] if agent else []),
        "decisions",
    ]


@pytest.mark.parametrize(
    "agent,converse,decisions,names",
    [
        (False, False, True, ["session", "decisions", "web"]),
        (True, False, False, ["session", "agent", "web"]),
        (True, False, True, ["session", "agent", "decisions", "web"]),
        (False, True, True, ["session", "decisions", "converse", "web"]),
        (True, True, True, ["session", "agent", "decisions", "converse", "web"]),
        (False, False, False, ["session", "web"]),
    ],
)
def test_start_ports_follow_all_selected_modes(agent, converse, decisions, names):
    ports = start_supervisor.required_service_ports(config_for(agent, converse, decisions))
    assert list(ports) == names
    if decisions:
        assert ports["decisions"] == 28763


@pytest.mark.parametrize("agent", [False, True])
def test_start_refuses_invalid_converse_without_decisions(agent):
    with pytest.raises(installation.InstallError, match="Converse requires Decisions"):
        start_supervisor.required_service_ports(config_for(agent, True, False))


def test_selected_decisions_port_cannot_alias_web():
    config = config_for(False, False, True)
    config["decisions_port"] = 8780
    with pytest.raises(installation.InstallError, match="distinct"):
        start_supervisor.required_service_ports(config)


@pytest.mark.parametrize("profile", [None, {"kind": "unsupported-fixture-kind"}])
def test_doctor_validates_selected_decisions_without_converse(profile):
    config = config_for(False, False, True)
    config["providers"] = {}
    if profile is not None:
        config["providers"]["decisions"] = {
            **profile,
            "base_url": "http://127.0.0.1:1235",
            "model": "synthetic-decisions",
        }
    result = doctor._check_provider_references(config, env={})
    assert result["status"] == "fail"
    assert "decisions" in result["summary"]


def test_doctor_reports_offline_selected_decisions_peer(monkeypatch):
    calls = []

    def listening(port):
        calls.append(port)
        return port != 28763

    monkeypatch.setattr(doctor, "_probe_loopback_port", listening)
    result = doctor._check_service_reachability(config_for(False, False, True), probe_services=True)
    assert calls == [8760, 28763]
    assert result["state"] == "offline"
    assert result["services"]["decisions"]["listening"] is False
    assert result["provider_connection"] == "unknown"


@pytest.mark.parametrize(
    "ready,expected",
    [(True, True), (False, False), (None, False), (1, False), ("true", False), ({}, False)],
)
def test_startup_requires_an_exact_decisions_readiness_boolean(monkeypatch, ready, expected):
    class Connection:
        closed = False
        status = 200

        def request(self, method, path):
            assert (method, path) == ("GET", "/health")

        def getresponse(self):
            return self

        def read(self, maximum):
            assert maximum == 16384
            return json.dumps({"service": "decisions", "routing_runtime_ready": ready}).encode()

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(start_supervisor.http.client, "HTTPConnection", lambda *a, **k: connection)
    assert start_supervisor._probe_health("decisions", 28763) is expected
    assert connection.closed
