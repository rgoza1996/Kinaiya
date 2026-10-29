"""Selected text lifecycle barriers; actual coordinator, synthetic workers only."""

import asyncio
from types import SimpleNamespace

import pytest

from services.resources.coordinator import (
    ResourceCoordinator,
    TextAdmissionError,
    WhistleAdmissionError,
)
from services.resources.state import ResourceState

MiB = 1024**2
BINDING = {
    "provider": "managed-gguf",
    "model": "public-synthetic-model",
    "profile": "public-synthetic-profile",
    "manifest_sha256": "a" * 64,
    "resources_url": "http://127.0.0.1:9999",
    "context_tokens": 2048,
    "max_new_tokens": 128,
}


class Manager:
    """Model Manager identity/result semantics without launching a process."""

    def __init__(self, workers=("gguf",)):
        self.rows = {
            name: {
                "state": "stopped",
                "ready": False,
                "owned_pid": None,
                "owned_pgid": None,
                "group_rss_bytes": 0,
                "stop_evidence": None,
            }
            for name in workers
        }
        self.identities = {}
        self.starts, self.stops = [], []
        self.start_hook = None
        self.sequence = 100
        self.close_calls = 0

    def status(self):
        return {"workers": {name: dict(row) for name, row in self.rows.items()}}

    def owned_identity(self, name):
        return self.identities.get(name)

    def is_current(self, identity):
        return identity is not None and self.identities.get(identity.worker_id) is identity

    async def start(self, name):
        self.starts.append(name)
        if self.start_hook is not None:
            await self.start_hook(name)
        self.sequence += 1
        identity = SimpleNamespace(worker_id=name, pid=self.sequence, start_time=self.sequence)
        self.identities[name] = identity
        self.rows[name].update(
            state="running",
            ready=True,
            owned_pid=identity.pid,
            owned_pgid=identity.pid,
            group_rss_bytes=64 * MiB,
            stop_evidence=None,
        )
        return {"ok": True, "ready": True, "pid": identity.pid, "worker_id": name}

    async def stop(self, name):
        identity = self.identities.pop(name, None)
        self.stops.append((name, identity))
        if identity is None:
            return {"ok": True, "worker_id": name, "already": True}
        proof = {
            "pid": identity.pid,
            "pgid": identity.pid,
            "reaped": True,
            "group_empty": True,
            "ownership_verified": True,
            "rss_before_bytes": 64 * MiB,
        }
        self.rows[name].update(
            state="stopped", ready=False, owned_pgid=None, group_rss_bytes=None, stop_evidence=proof
        )
        return {"ok": True, "worker_id": name, "stop_evidence": proof}

    async def stop_if_current(self, identity):
        if not self.is_current(identity):
            return {"ok": True, "worker_id": identity.worker_id, "superseded": True}
        return await self.stop(identity.worker_id)

    async def close(self):
        self.close_calls += 1
        for name in self.rows:
            await self.stop(name)


class Control:
    def __init__(self, binding):
        self.binding = dict(binding)
        self.value = True

    async def health(self):
        return {
            "provider": {
                "provider": self.binding["provider"],
                "model": self.binding["model"],
                "local": True,
                "streaming": True,
            },
            "managed_text": dict(self.binding),
        }

    async def enabled(self, value):
        self.value = value

    async def close(self):
        pass


class Hooks:
    """Trusted callbacks model the existing broker and endpoint's retained work."""

    def __init__(self):
        self.aborts = 0
        self.drains = 0
        self.releases = []
        self.allowed = True
        self.admission = None
        self.drain_entered, self.http_closed = asyncio.Event(), asyncio.Event()
        self.http_closed.set()
        self.release_gate = None
        self.drain_error = None

    def abort(self):
        self.aborts += 1

    async def drain(self):
        self.drains += 1
        self.drain_entered.set()
        if self.admission is not None:
            await asyncio.gather(asyncio.shield(self.admission), return_exceptions=True)
        await self.http_closed.wait()
        if self.drain_error is not None:
            raise self.drain_error

    async def release(self, identity, result):
        self.releases.append((identity, result))
        if self.release_gate is not None:
            await self.release_gate.wait()
        return self.allowed


def environment(tmp_path, *, workers=("gguf",), selected="gguf", binding=None):
    manager = Manager(workers)
    binding = dict(BINDING if binding is None else binding)
    control = Control(binding)
    memory, observations = [1024 * MiB], []

    def probe():
        observations.append(memory[0])
        return memory[0]

    coordinator = ResourceCoordinator(
        manager,
        ResourceState(tmp_path / "control/resources.json"),
        control,
        text_worker_id=selected,
        managed_text_binding=binding,
        memory_probe=probe,
        text_restore_reserve_bytes=512 * MiB,
        whistle_restore_reserve_bytes=512 * MiB,
    )
    hooks = Hooks()
    coordinator.register_text_lifecycle(abort=hooks.abort, drain=hooks.drain, release=hooks.release)
    return SimpleNamespace(
        coordinator=coordinator,
        manager=manager,
        hooks=hooks,
        control=control,
        memory=memory,
        observations=observations,
    )


async def action(env, operation, key, **fields):
    return await env.coordinator.action({"operation": operation, "idempotency_key": key, **fields})


async def wait_until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def finish(env):
    env.hooks.allowed = True
    env.hooks.http_closed.set()
    if env.hooks.release_gate is not None:
        env.hooks.release_gate.set()
    await env.coordinator.close()


@pytest.mark.parametrize(
    "workers,selected,binding",
    [
        (("qwen3", "gguf"), "gguf", BINDING),
        (("qwen3",), "gguf", BINDING),
        (("gguf",), "qwen3", BINDING),
        (("gguf",), "gguf", None),
        (("gguf",), "gguf", {**BINDING, "provider": "managed-qwen3"}),
    ],
)
def test_rejects_ambiguous_or_unbound_native_selection(tmp_path, workers, selected, binding):
    state = ResourceState(tmp_path / "control/resources.json")
    try:
        with pytest.raises(ValueError):
            ResourceCoordinator(
                Manager(workers),
                state,
                Control(BINDING),
                text_worker_id=selected,
                managed_text_binding=binding,
            )
    finally:
        state.close()


async def test_native_hooks_register_once_and_identity_is_not_serialized(tmp_path):
    env = environment(tmp_path)
    try:
        with pytest.raises(ValueError):
            env.coordinator.register_text_lifecycle(
                abort=env.hooks.abort, drain=env.hooks.drain, release=env.hooks.release
            )
        await env.coordinator.initialize()
        old = env.manager.owned_identity("gguf")
        result = await env.coordinator.stop_text()
        assert env.hooks.releases[-1] == (old, result)
        assert result["stop_evidence"]["pid"] == old.pid and "identity" not in result
        again = await env.coordinator.stop_text()
        assert again["release_confirmed"] and again["stop_evidence"] == result["stop_evidence"]
        assert env.hooks.releases[-1][0] is None
    finally:
        await finish(env)


async def test_stale_cleanup_never_releases_replacement_without_endpoint_proof(tmp_path):
    env = environment(tmp_path)
    try:
        await env.coordinator.initialize()
        old = env.manager.owned_identity("gguf")
        await env.coordinator.stop_text(old)
        current = await env.coordinator.admit_text(env.coordinator.text_policy())
        env.hooks.allowed = False
        stops = list(env.manager.stops)
        with pytest.raises(RuntimeError, match="endpoint release"):
            await env.coordinator.stop_text(old)
        assert env.hooks.releases[-1][0] is old
        assert env.hooks.releases[-1][1]["superseded"]
        assert env.manager.stops == stops and env.manager.is_current(current)
    finally:
        await finish(env)


@pytest.mark.parametrize("failure", ["exception", "cancel", "not-ready"])
async def test_failed_start_releases_granted_endpoint_without_draining_its_broker(
    tmp_path, failure
):
    env = environment(tmp_path)
    try:
        await env.coordinator.initialize()
        await env.coordinator.stop_text()
        start = env.manager.start
        granted = []

        async def uncertain(name):
            await start(name)
            granted.append(env.manager.owned_identity(name))
            if failure == "cancel":
                raise asyncio.CancelledError
            if failure == "exception":
                raise RuntimeError("Synthetic startup failure")
            return {"ok": False, "ready": False}

        env.manager.start = uncertain
        error = asyncio.CancelledError if failure == "cancel" else TextAdmissionError
        with pytest.raises(error) as caught:
            await env.coordinator.admit_text(env.coordinator.text_policy())
        if failure != "cancel":
            assert caught.value.cleanup_confirmed
        assert env.manager.owned_identity("gguf") is None
        assert env.hooks.releases[-1][0] is granted[0] and env.hooks.drains == 0
    finally:
        await finish(env)


async def test_unknown_old_endpoint_prevents_fresh_start_without_broker_drain(tmp_path):
    env = environment(tmp_path)
    try:
        await env.coordinator.initialize()
        await env.manager.stop("gguf")
        env.hooks.allowed = False
        starts = list(env.manager.starts)
        with pytest.raises(TextAdmissionError) as caught:
            await env.coordinator.admit_text(env.coordinator.text_policy())
        assert not caught.value.cleanup_confirmed
        assert env.manager.starts == starts and env.hooks.drains == 0
        env.hooks.allowed = True
        recovered = await env.coordinator.admit_text(env.coordinator.text_policy())
        assert env.manager.is_current(recovered) and env.hooks.drains == 0
    finally:
        await finish(env)


@pytest.mark.parametrize("operation", ["off", "handoff"])
async def test_owner_action_waits_delayed_admission_and_real_http_close(tmp_path, operation):
    env = environment(tmp_path)
    entered, permit = asyncio.Event(), asyncio.Event()
    pending = None
    try:
        await env.coordinator.initialize()
        if operation == "handoff":
            assert (await action(env, "profile", "profile", profile="local_work_priority"))["ok"]
        await env.manager.stop("gguf")

        async def delayed(_):
            entered.set()
            await permit.wait()

        env.manager.start_hook = delayed
        env.hooks.admission = asyncio.create_task(
            env.coordinator.admit_text(env.coordinator.text_policy())
        )
        await asyncio.wait_for(entered.wait(), 1)
        env.hooks.http_closed.clear()
        pending = asyncio.create_task(
            action(env, "converse", "off", enabled=False)
            if operation == "off"
            else action(env, "handoff", "handoff", required_bytes=128 * MiB)
        )
        await asyncio.wait_for(env.hooks.drain_entered.wait(), 1)
        assert env.coordinator.lock.locked() and not pending.done()
        permit.set()
        with pytest.raises(TextAdmissionError) as caught:
            await asyncio.wait_for(asyncio.shield(env.hooks.admission), 1)
        assert caught.value.cleanup_confirmed and env.manager.owned_identity("gguf") is None
        assert not pending.done()
        env.hooks.http_closed.set()
        result = await asyncio.wait_for(pending, 1)
        assert result["ok"] and result["policy"]["phase"] == (
            "off" if operation == "off" else "local_work"
        )
        assert env.hooks.aborts == env.hooks.drains == 1
        assert len(env.manager.starts) == 2
        assert any(identity is not None for identity, _ in env.hooks.releases)
    finally:
        permit.set()
        env.hooks.http_closed.set()
        await asyncio.gather(*([pending] if pending else []), return_exceptions=True)
        await finish(env)


async def test_nested_release_failure_blocks_owner_success_and_restoration(tmp_path):
    env = environment(tmp_path)
    try:
        await env.coordinator.initialize()
        env.hooks.allowed = False
        denied = await action(env, "converse", "off", enabled=False)
        assert not denied["ok"] and denied["policy"]["phase"] == "failed"
        assert env.manager.owned_identity("gguf") is None
        assert env.manager.rows["gguf"]["stop_evidence"]["group_empty"]
        starts = list(env.manager.starts)
        denied = await action(env, "converse", "on", enabled=True)
        assert not denied["ok"] and env.manager.starts == starts
    finally:
        await finish(env)


async def test_shutdown_fences_admission_and_always_closes_manager_on_failed_drain(tmp_path):
    env = environment(tmp_path)
    await env.coordinator.initialize()
    policy = env.coordinator.text_policy()
    env.hooks.drain_error = RuntimeError("Synthetic held HTTP cleanup failure")
    with pytest.raises(RuntimeError, match="Synthetic"):
        await env.coordinator.close()
    assert env.manager.close_calls == 1 and env.coordinator.state.record.phase == "failed"
    with pytest.raises(TextAdmissionError):
        await env.coordinator.admit_text(policy)
    await env.coordinator.close()
    assert env.manager.close_calls == 1


async def test_shutdown_waits_http_owner_and_preserves_first_stop_identity(tmp_path):
    env = environment(tmp_path)
    closing = None
    try:
        await env.coordinator.initialize()
        old = env.manager.owned_identity("gguf")
        policy = env.coordinator.text_policy()
        env.hooks.http_closed.clear()
        closing = asyncio.create_task(env.coordinator.close())
        await asyncio.wait_for(env.hooks.drain_entered.wait(), 1)
        assert not closing.done() and env.manager.owned_identity("gguf") is None
        with pytest.raises(TextAdmissionError):
            await env.coordinator.admit_text(policy)
        env.hooks.http_closed.set()
        await asyncio.wait_for(closing, 1)
        assert env.manager.close_calls == 1
        released = [identity for identity, _ in env.hooks.releases]
        assert old in released and env.hooks.aborts == env.hooks.drains == 1
    finally:
        env.hooks.http_closed.set()
        await asyncio.gather(*([closing] if closing else []), return_exceptions=True)
        await finish(env)


async def test_whistle_and_text_memory_observations_are_serialized(tmp_path):
    env = environment(tmp_path, workers=("gguf", "whistle"))
    entered, permit, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    text = whistle = None
    try:
        await env.coordinator.initialize()
        await env.manager.stop("gguf")
        await env.manager.stop("whistle")

        async def delayed(name):
            if name == "gguf":
                entered.set()
                await permit.wait()

        env.manager.start_hook = delayed
        text = asyncio.create_task(env.coordinator.admit_text(env.coordinator.text_policy()))
        await asyncio.wait_for(entered.wait(), 1)
        observed = len(env.observations)
        env.memory[0] = 400 * MiB

        async def recover_whistle():
            waiting.set()
            return await env.coordinator.admit_whistle(env.coordinator.whistle_policy())

        whistle = asyncio.create_task(recover_whistle())
        await asyncio.wait_for(waiting.wait(), 1)
        assert not whistle.done() and len(env.observations) == observed
        permit.set()
        assert env.manager.is_current(await asyncio.wait_for(text, 1))
        with pytest.raises(WhistleAdmissionError):
            await asyncio.wait_for(whistle, 1)
        assert env.manager.starts == ["gguf", "whistle", "gguf"]
    finally:
        permit.set()
        await asyncio.gather(*[task for task in (text, whistle) if task], return_exceptions=True)
        await finish(env)


async def test_native_restore_preserves_resident_credit_and_replay_has_no_stop_effect(tmp_path):
    env = environment(tmp_path)
    try:
        await env.coordinator.initialize()
        current = env.manager.owned_identity("gguf")
        env.coordinator.restore_reserve = 800 * MiB
        env.memory[0] = 400 * MiB
        assert (await action(env, "restore", "resident"))["ok"]
        assert env.manager.owned_identity("gguf") is current
        env.memory[0] = 1024 * MiB
        off = await action(env, "converse", "old-off", enabled=False)
        assert off["ok"] and (await action(env, "converse", "on", enabled=True))["ok"]
        current = env.manager.owned_identity("gguf")
        counts = env.hooks.aborts, env.hooks.drains, len(env.manager.stops)
        assert await action(env, "converse", "old-off", enabled=False) == off
        with pytest.raises(ValueError, match="conflicts"):
            await action(env, "converse", "old-off", enabled=True)
        assert (env.hooks.aborts, env.hooks.drains, len(env.manager.stops)) == counts
        assert env.manager.owned_identity("gguf") is current
    finally:
        await finish(env)


async def test_restore_waits_admission_cleanup_before_resident_credit_snapshot(tmp_path):
    env = environment(tmp_path)
    entered, permit = asyncio.Event(), asyncio.Event()
    admission = restore = None
    try:
        await env.coordinator.initialize()
        await env.coordinator.stop_text()

        async def delayed(_):
            entered.set()
            await permit.wait()

        env.manager.start_hook = delayed
        admission = asyncio.create_task(env.coordinator.admit_text(env.coordinator.text_policy()))
        await asyncio.wait_for(entered.wait(), 1)
        env.coordinator.restore_reserve = 800 * MiB
        env.memory[0] = 400 * MiB
        env.hooks.release_gate = asyncio.Event()
        restore = asyncio.create_task(action(env, "restore", "racing-restore"))
        await wait_until(lambda: env.coordinator.state.record.phase == "restoring")
        observations = len(env.observations)
        releases = len(env.hooks.releases)
        permit.set()
        # The old allocation stops, but its endpoint remains owned until release.
        await wait_until(lambda: len(env.hooks.releases) > releases)
        assert env.manager.owned_identity("gguf") is None
        assert not restore.done() and len(env.observations) == observations
        env.hooks.release_gate.set()
        with pytest.raises(TextAdmissionError) as caught:
            await asyncio.wait_for(admission, 1)
        assert caught.value.cleanup_confirmed
        result = await asyncio.wait_for(restore, 1)
        assert not result["ok"] and result["policy"]["phase"] == "failed"
        assert env.manager.owned_identity("gguf") is None and len(env.manager.starts) == 2
        assert env.hooks.drains == 0
    finally:
        permit.set()
        if env.hooks.release_gate is not None:
            env.hooks.release_gate.set()
        await asyncio.gather(
            *[task for task in (admission, restore) if task], return_exceptions=True
        )
        await finish(env)


async def test_changed_protected_binding_fails_before_native_restore(tmp_path):
    env = environment(tmp_path)
    try:
        env.control.binding["context_tokens"] = "2048"
        await env.coordinator.initialize()
        assert env.coordinator.state.record.phase == "failed" and not env.manager.starts
        assert env.coordinator.local_converse_unmanaged
    finally:
        await finish(env)
