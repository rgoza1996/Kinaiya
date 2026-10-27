from services.launcher import supervise


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
