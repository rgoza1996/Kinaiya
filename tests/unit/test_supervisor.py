import pytest

from services.launcher import supervise


@pytest.mark.parametrize("source_service", ["decisions", "converse"])
def test_control_source_cannot_be_an_agent_workspace(tmp_path, monkeypatch, source_service):
    import json
    from types import SimpleNamespace

    import uvicorn

    from services import launcher

    root = tmp_path / "source"
    workspace = root / "services" / source_service
    workspace.mkdir(parents=True)
    data = tmp_path / "owner-data"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"session_url": "http://127.0.0.1:12345"}))
    monkeypatch.setattr(launcher, "ROOT", root)
    monkeypatch.setattr(
        uvicorn, "run", lambda *args, **kwargs: pytest.fail("unsafe workspace was served")
    )
    args = SimpleNamespace(
        data_dir=data, config=config, service="agent", workspace=workspace, port=None
    )
    with pytest.raises(SystemExit, match="disjoint"):
        launcher.serve(args)


def test_unloading_converse_does_not_terminate_agent_or_storage():
    children, stopped = {}, []

    class Child:
        def __init__(self, name):
            self.name = name
            self.code = 0 if name == "converse" else None

        def poll(self):
            return self.code

        def send_signal(self, signal):
            stopped.append(self.name)
            self.code = 0

        def wait(self, timeout):
            return self.code

    def spawn(command):
        child = Child(command[0])
        children[child.name] = child
        return child

    def check(_):
        assert children["agent"].poll() is None
        assert children["session"].poll() is None
        assert stopped == []
        raise KeyboardInterrupt

    supervise({name: [name] for name in ("converse", "agent", "session")}, popen=spawn, sleep=check)
    assert set(stopped) == {"agent", "session"}
