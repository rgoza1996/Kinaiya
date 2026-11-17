"""Real, opt-in Pi SDK integration in a private network and filesystem namespace."""

import asyncio
import json
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from packages.agent_adapters.pi_sdk import PiPeerUnavailable, PiSdkPeer
from packages.contracts.external_agent import ExternalAgentRevoke
from packages.contracts.models import SessionRequest
from tests.integration.test_external_agent_session import add, bind_to, connection, native

ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = os.environ.get("KINAIYA_PI_TEST_SDK_ROOT")
pytestmark = pytest.mark.skipif(
    not SDK_ROOT,
    reason="requires the hash-verified Pi SDK installation prefix in KINAIYA_PI_TEST_SDK_ROOT",
)


def sdk_sandbox(private, previous=None, *, transcript=False):
    node = shutil.which("node")
    assert node and shutil.which("bwrap"), "Pi qualification requires Node and bubblewrap"
    sdk = Path(SDK_ROOT).resolve(strict=True)
    args = [
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--ro-bind",
        str(Path(node).resolve()),
        "/node",
        "--ro-bind",
        str(sdk),
        "/sdk",
        "--ro-bind",
        str(ROOT / "packages/agent_adapters/pi_peer.mjs"),
        "/peer.mjs",
        "--bind",
        str(private),
        "/private",
        "--clearenv",
        "--setenv",
        "HOME",
        "/private/home",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--chdir",
        "/private/workspace",
        "/node",
        "--no-warnings",
        "/peer.mjs",
        *(["--transcript"] if transcript else []),
        "/sdk",
        "/private/workspace",
        "/private/agent",
        "/private/sessions",
    ]
    if previous:
        args.append("/private/" + previous.relative_to(private).as_posix())
    return args


@pytest.fixture
def private(tmp_path):
    tmp_path = tmp_path / "pi"
    tmp_path.mkdir(mode=0o700)
    for relative in ("home", "workspace", "agent/extensions", "sessions"):
        (tmp_path / relative).mkdir(mode=0o700, parents=True)
    shutil.copyfile(
        ROOT / "tests/fixtures/pi_bridge_provider.mjs",
        tmp_path / "agent/extensions/fixture.js",
    )
    (tmp_path / "agent/settings.json").write_text(
        json.dumps(
            {
                "defaultProvider": "kinaiya-bridge-fixture",
                "defaultModel": "fixture",
                "retry": {"enabled": False},
            }
        )
    )
    return tmp_path


@asynccontextmanager
async def connect(private, previous=None, *, transcript=False):
    process = await asyncio.create_subprocess_exec(
        *sdk_sandbox(private, previous, transcript=transcript),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=2_097_152,
    )
    try:
        yield process
    finally:
        if process.returncode is None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()


async def receive(process):
    raw = await asyncio.wait_for(process.stdout.readline(), 10)
    assert raw, (await process.stderr.read()).decode()
    frame = json.loads(raw)
    assert frame["version"] == 1
    return frame


async def request(process, command, sequence, **fields):
    identifier = f"{sequence:032x}"
    frame = {"version": 1, "command": command, "id": identifier, **fields}
    process.stdin.write(json.dumps(frame).encode() + b"\n")
    await process.stdin.drain()
    return identifier


async def read_through(process, predicate):
    frames = []
    while True:
        try:
            frame = await receive(process)
        except TimeoutError:
            pytest.fail(f"Pi did not emit the expected frame after {frames!r}")
        frames.append(frame)
        if predicate(frame):
            return frames
        assert len(frames) < 40


async def close(process, sequence):
    stop_id = await request(process, "close", sequence)
    reply = await receive(process)
    assert (reply["id"], reply["disposition"]) == (stop_id, "stopping")
    assert await asyncio.wait_for(process.wait(), 5) == 0


async def test_real_pi_preserves_literal_instruction_and_reopens_without_replay(private):
    instruction = "/fixture-command literal input\r\nSecond \u2028line"
    settings_path = private / "agent/settings.json"
    original_settings = settings_path.read_bytes()
    async with connect(private) as process:
        ready = await receive(process)
        assert ready["type"] == "ready" and ready["sdk_version"] == "1.5.2"
        initial_session = ready["session_id"]
        run_id = await request(process, "submit", 1, text=instruction)
        result = await read_through(process, lambda frame: frame.get("kind") == "settled")
        assert [frame["disposition"] for frame in result if frame["type"] == "response"] == [
            "accepted"
        ]
        assert [
            (frame["run_id"], frame["text"])
            for frame in result
            if frame.get("kind") == "assistant_message"
        ] == [(run_id, instruction)]
        await request(process, "submit", 1, text=instruction)
        assert (await receive(process))["reason"] == "duplicate_request"
        await close(process, 2)

    (session_file,) = (private / "sessions").glob("*.jsonl")
    async with connect(private, session_file) as process:
        assert (await receive(process))["session_id"] == initial_session
        await request(process, "status", 3)
        status = await receive(process)
        assert status["disposition"] == "observed" and status["busy"] is False
        assert status["run_id"] is None
        await close(process, 4)
    assert settings_path.read_bytes() == original_settings


async def test_real_pi_routes_interrupt_to_the_active_run_and_reports_backend_failure(private):
    async with connect(private) as process:
        assert (await receive(process))["type"] == "ready"
        run_id = await request(process, "submit", 1, text="hold for interrupt")
        started = await read_through(process, lambda frame: frame.get("kind") == "running")
        assert any(frame.get("disposition") == "accepted" for frame in started)

        await request(process, "interrupt", 2, run_id="f" * 32)
        assert (await receive(process))["reason"] == "run_changed"
        stop_id = await request(process, "interrupt", 3, run_id=run_id)
        stopped = await read_through(process, lambda frame: frame.get("kind") == "interrupted")
        assert any(
            frame.get("id") == stop_id and frame.get("disposition") == "requested"
            for frame in stopped
        )
        assert stopped[-1]["run_id"] == run_id

        failed_id = await request(process, "submit", 4, text="synthetic provider failure")
        failed = await read_through(process, lambda frame: frame.get("kind") == "failed")
        assert failed[-1]["run_id"] == failed_id
        await close(process, 5)


async def test_real_pi_refuses_a_missing_exact_owner_selection(private):
    settings = private / "agent/settings.json"
    config = json.loads(settings.read_text())
    config["defaultModel"] = "not-registered"
    settings.write_text(json.dumps(config))
    async with connect(private) as process:
        with pytest.raises(PiPeerUnavailable, match="Pi-selected model is unavailable"):
            await PiSdkPeer.attach(process.stdout, process.stdin)
        assert await asyncio.wait_for(process.wait(), 5) == 1


async def test_python_bridge_transport_preserves_one_real_pi_submission(private):
    instruction = "Python owner through the real Pi SDK \u2602"
    async with connect(private) as process:
        peer = await PiSdkPeer.attach(process.stdout, process.stdin)
        original = "1" * 32
        first, retained = await asyncio.gather(
            peer.submit(original, instruction),
            peer.submit(original, instruction),
        )
        assert first == retained and first.disposition == "accepted"
        async with asyncio.timeout(5):
            while not any(event.kind == "settled" for event in peer.events(0)):
                await asyncio.sleep(0.02)
        output = [event for event in peer.events(0) if event.kind == "assistant_message"]
        assert [(event.run_id, event.text) for event in output] == [(original, instruction)]
        assert (await peer.status("2" * 32)).busy is False
        assert (await peer.request_close("3" * 32)).disposition == "stopping"
        assert await asyncio.wait_for(process.wait(), 5) == 0


async def test_real_pi_requests_transcript_on_demand_from_authenticated_session(private):
    async with native(private.parent / "journal.sqlite") as peers:
        session = peers.store.create_session(SessionRequest(channel="converse"))
        visible = add(peers.store, session, "conversation.user", "Authorized history ☂")
        add(peers.store, session, "conversation.user", "Private history", scope="local_only")
        later = add(
            peers.store, session, "conversation.converse", "Authorized reply.", role="converse"
        )
        consent = await peers.owner.authorize(connection(session, backend=None))
        bound = None
        reads = []

        async def read_transcript(**position):
            reads.append(position)
            return await peers.decisions.read_bound_transcript(bound, **position)

        async with connect(private, transcript=True) as process:
            peer = await PiSdkPeer.attach(
                process.stdout, process.stdin, transcript_reader=read_transcript
            )
            bound = await peers.decisions.bind(bind_to(consent, peer.session_id))

            async def run(identifier, text):
                assert (await peer.submit(identifier, text)).disposition == "accepted"
                async with asyncio.timeout(5):
                    while True:
                        current = [event for event in peer.events(0) if event.run_id == identifier]
                        if any(event.kind == "settled" for event in current):
                            return [
                                event.text for event in current if event.kind == "assistant_message"
                            ]
                        await asyncio.sleep(0.02)

            assert await run("1" * 32, "ordinary turn") == ["ordinary turn"]
            assert reads == []
            (answer,) = await run("2" * 32, "read scoped transcript")
            assert len(reads) == 3
            page = json.loads(answer)
            assert [(entry["event_id"], entry["text"]) for entry in page["entries"]] == [
                (visible.event_id, "Authorized history ☂"),
                (later.event_id, "Authorized reply."),
            ]
            assert page["has_more"] is False

            await peers.owner.revoke(
                ExternalAgentRevoke(
                    contract_version=1,
                    request_id="revoke-test-grant",
                    operation="revoke",
                    attachment_id=consent.attachment_id,
                )
            )
            (answer,) = await run("3" * 32, "read scoped transcript")
            assert len(reads) == 4 and "Converse transcript page is unavailable" in answer
            assert (await peer.request_close("4" * 32)).disposition == "stopping"
            assert await asyncio.wait_for(process.wait(), 5) == 0
